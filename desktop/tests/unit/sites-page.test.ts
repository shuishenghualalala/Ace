import { describe, expect, it, vi } from 'vitest';

vi.mock('electron', () => ({ protocol: { handle: vi.fn() } }));

import type { LocalSite, SiteAnnotation } from '../../src/ui/backend-client';
import { parseSitePreviewUrl } from '../../src/main/site-preview-protocol';
import { buildSiteAnnotationPrompt } from '../../src/ui/features/sites-page';

describe('sites page annotation handoff', () => {
  it('parses only controlled inspiration protocol ids and keeps paths encoded', () => {
    expect(parseSitePreviewUrl('ace-site://site_0123456789ab/assets/app.js')).toMatchObject({
      kind: 'site', siteId: 'site_0123456789ab', assetPath: 'assets/app.js',
    });
    expect(parseSitePreviewUrl('ace-site://canvas_abcdef123456/')).toMatchObject({
      kind: 'canvas', canvasId: 'canvas_abcdef123456', assetPath: '',
    });
    expect(parseSitePreviewUrl('ace-site://widget_0123456789ab/%2Fetc')).toMatchObject({
      kind: 'widget', assetPath: '%2Fetc',
    });
    expect(parseSitePreviewUrl('https://example.com')).toBeNull();
    expect(parseSitePreviewUrl('ace-site://canvas_not-an-id/')).toBeNull();
  });
  it('includes stable source and DOM context and forbids implicit republish', () => {
    const site = {
      id: 'site_1', workspace_id: 'ws_1', session_id: 'session_1', name: 'Demo', description: 'Demo App',
      source_path: '/workspace/demo', build_command: 'npm run build', output_directory: 'dist',
      active_release_id: 'rel_1', created_at: 1, updated_at: 2,
    } satisfies LocalSite;
    const annotation = {
      id: 'ann_1', site_id: 'site_1', release_id: 'rel_1', route: '/pricing',
      selector: 'main > h1', element_tag: 'h1', element_text: 'Price', comment: '改成中文',
      context: {}, status: 'open', created_at: 1, updated_at: 1,
    } satisfies SiteAnnotation;

    const prompt = buildSiteAnnotationPrompt(site, annotation);
    expect(prompt).toContain('/workspace/demo');
    expect(prompt).toContain('main > h1');
    expect(prompt).toContain('改成中文');
    expect(prompt).toContain('不要自动发布');
  });

  it('keeps the create entry and Inspiration session marker in the Desktop shell', async () => {
    const fs = await import('node:fs/promises');
    const [shell, sitesPage, workspaces] = await Promise.all([
      fs.readFile('assets/index.html', 'utf8'),
      fs.readFile('src/ui/features/sites-page.ts', 'utf8'),
      fs.readFile('src/ui/features/workspaces.ts', 'utf8'),
    ]);
    expect(sitesPage).toContain('data-sites-create');
    expect(shell).toContain('id="chat-sites-mode"');
    expect(shell).toContain('data-sites-logo');
    expect(workspaces).toContain('data-sites-logo');
    expect(shell).toContain('灵感');
    expect(shell).toContain('设计一个 App');
    expect(shell).not.toContain('chat-sites-mode__logo" aria-hidden="true"><i>');
  });

  it('keeps one Sites mount and a valid trash icon in the static shell', async () => {
    const fs = await import('node:fs/promises');
    const [shell, sprite, styles, wikiStyles] = await Promise.all([
      fs.readFile('assets/index.html', 'utf8'),
      fs.readFile('assets/crew-ui-symbols.svg', 'utf8'),
      fs.readFile('assets/styles/sites-page.css', 'utf8'),
      fs.readFile('assets/styles/wiki-page.css', 'utf8'),
    ]);
    expect(shell.match(/id="sites-tab"/g)).toHaveLength(1);
    expect(shell.match(/id="sites-page-root"/g)).toHaveLength(1);
    expect(sprite).toContain('<symbol id="icon-trash" viewBox="0 0 24 24">');
    expect(styles).not.toMatch(/#[0-9a-f]{3,8}/i);
    expect(styles).not.toMatch(/--(?:bg-primary|surface|text-primary|text-secondary|v2-|color-accent)/);
    expect(styles).toContain('var(--mw-inspector-hard-max)');
    expect(wikiStyles).toContain('--mw-symbol-line: var(--mw-identity-blue)');
    expect(wikiStyles).not.toContain('--crew-line');
    expect(wikiStyles).not.toContain('#2463eb');
  });

  it('uses one authenticated preview protocol for every inspiration', async () => {
    const fs = await import('node:fs/promises');
    const sitesPage = await fs.readFile('src/ui/features/sites-page.ts', 'utf8');
    expect(sitesPage).toContain('ace-site://${encodeURIComponent(item.id)}/');
    expect(sitesPage).not.toContain('ace-site://preview/');
    expect(sitesPage).not.toContain('http://127.0.0.1:8000');
    expect(sitesPage).toContain('backendApi.inspirations()');
    expect(sitesPage).toContain('loading="lazy"');
    expect(sitesPage).toContain('正在打开 App');
    expect(sitesPage).toContain('ace-site-preview-ready');
    expect(sitesPage).toContain('ace-site-preview-error');
  });

  it('opens annotation mode in the bound conversation and batches comments in the composer', async () => {
    const fs = await import('node:fs/promises');
    const [sitesPage, uiIndex, chatController, shell] = await Promise.all([
      fs.readFile('src/ui/features/sites-page.ts', 'utf8'),
      fs.readFile('src/ui/app.ts', 'utf8'),
      fs.readFile('src/ui/features/chat-controller.ts', 'utf8'),
      fs.readFile('assets/index.html', 'utf8'),
    ]);
    expect(uiIndex).toContain('await openSession(item.sessionId)');
    expect(sitesPage).toContain('ace.inspirationAnnotationDrafts.v1');
    expect(sitesPage).toContain('ace.siteAnnotationDrafts.v1');
    expect(sitesPage).toContain('条批注');
    expect(sitesPage).toContain('ace-site-element-selected');
    expect(chatController).toContain('composeSiteAnnotationMessage(sessionId, plainContent)');
    expect(chatController).toContain('clearSiteAnnotationDraft(sessionId);');
    expect(shell).toContain('id="chat-site-annotation-preview"');
    expect(shell).toContain('id="site-annotation-button"');
    expect(sitesPage).toContain("site.session_id === sessionId");
    expect(sitesPage).toContain('activeSessionSites.length === 1');
  });

  it('keeps asynchronous inspector refreshes from replacing the annotation workbench', async () => {
    const fs = await import('node:fs/promises');
    const inspector = await fs.readFile('src/ui/features/inspector.ts', 'utf8');
    expect(inspector).toContain('let customViewOpen = false');
    expect(inspector).toContain('customViewOpen = true');
    expect(inspector).toContain('if (customViewOpen)');
  });

  it('registers the authenticated site preview protocol in the main process', async () => {
    const fs = await import('node:fs/promises');
    const [main, protocolSource, shell] = await Promise.all([
      fs.readFile('src/main/index.ts', 'utf8'),
      fs.readFile('src/main/site-preview-protocol.ts', 'utf8'),
      fs.readFile('assets/index.html', 'utf8'),
    ]);
    expect(main).toContain('registerSitePreviewProtocol');
    expect(protocolSource).toContain('/api/sites/${resolved.siteId}/preview/');
    expect(protocolSource).toContain('headers: gateway.headers(endpoint.pathname)');
    expect(protocolSource).toContain('previewErrorDocument');
    expect(shell).toContain("frame-src 'self' file: ace-site:");
  });

  it('allows user-triggered downloads in every site preview surface', async () => {
    const fs = await import('node:fs/promises');
    const sitesPage = await fs.readFile('src/ui/features/sites-page.ts', 'utf8');
    expect(sitesPage.match(/sandbox=\"allow-scripts allow-forms allow-modals allow-same-origin allow-downloads\"/g)).toHaveLength(2);
  });

  it('does not reset the main window reveal gate for site frame navigation', async () => {
    const fs = await import('node:fs/promises');
    const main = await fs.readFile('src/main/index.ts', 'utf8');
    expect(main).not.toContain("webContents.on('did-start-loading'");
    expect(main).toContain("webContents.on('did-start-navigation'");
    expect(main).toContain('if (!isMainFrame || !isTrustedRendererFileUrl');
    expect(main).toContain('!isTrustedRendererFileUrl(url, expected, RENDERER_LAUNCH_SEARCH)');
  });

  it('provides a mixed visual gallery and unified lifecycle actions', async () => {
    const fs = await import('node:fs/promises');
    const [sitesPage, backend, styles, protocolSource] = await Promise.all([
      fs.readFile('src/ui/features/sites-page.ts', 'utf8'),
      fs.readFile('src/ui/backend-client.ts', 'utf8'),
      fs.readFile('assets/styles/sites-page.css', 'utf8'),
      fs.readFile('src/main/site-preview-protocol.ts', 'utf8'),
    ]);
    expect(sitesPage).not.toContain("pageMode: 'sites' | 'canvases'");
    expect(sitesPage).toContain('data-inspiration-search');
    expect(sitesPage).toContain('data-inspiration-modify');
    expect(sitesPage).toContain('data-inspiration-share');
    expect(sitesPage).toContain('data-inspiration-pin');
    expect(sitesPage).toContain('data-inspiration-delete');
    expect(sitesPage).toContain('window.Crew?.openInspirationWindow');
    expect(backend).toContain('/api/sites/inspirations');
    expect(styles).toMatch(/grid-template-columns:\s*repeat\(auto-fill/);
    expect(styles).toMatch(/\.sites-page-root\s*\{/);
    expect(styles).toMatch(/\.inspiration-detail\s*\{[\s\S]*width:\s*100%;[\s\S]*height:\s*100%;/);
    expect(styles).toMatch(/\.inspiration-detail__stage\s*\{[\s\S]*width:\s*100%;[\s\S]*flex:\s*1 1 0;/);
    expect(protocolSource).toContain("kind: 'site' | 'canvas' | 'widget'");
    expect(protocolSource).toContain('/api/sites/canvases/${resolved.canvasId}/render');
    expect(protocolSource).toContain('/api/sites/widgets/${resolved.widgetId}/render/');
  });

  it('keeps pin windows and ZIP saves behind constrained main-process APIs', async () => {
    const fs = await import('node:fs/promises');
    const [main, preload, stickyPreload, sitesPage, build, schemas] = await Promise.all([
      fs.readFile('src/main/index.ts', 'utf8'),
      fs.readFile('src/main/preload.ts', 'utf8'),
      fs.readFile('src/main/inspiration-sticky-preload.ts', 'utf8'),
      fs.readFile('src/ui/features/sites-page.ts', 'utf8'),
      fs.readFile('esbuild.config.mjs', 'utf8'),
      fs.readFile('src/shared/ipc-schemas.ts', 'utf8'),
    ]);
    expect(main).toContain('const inspirationWindows = new Map<string, BrowserWindow>()');
    expect(main).toContain('alwaysOnTop: true');
    expect(main).toContain('frame: false');
    expect(main).toContain('skipTaskbar: true');
    expect(main).toContain("process.platform === 'darwin'");
    expect(main).toContain("ipcMain.on('inspiration:sticky-close'");
    expect(main).toContain("mainWindow.webContents.send('inspiration:window-state-changed'");
    expect(main).toContain('if (!win.isDestroyed()) win.destroy()');
    expect(main).toContain('sandbox: true');
    expect(main).toContain("setWindowOpenHandler(() => ({ action: 'deny' }))");
    expect(main).toContain('for (const win of inspirationWindows.values())');
    expect(main).toContain('inspirationWindows.clear()');
    expect(main).toContain("segments[1] === 'exports'");
    expect(preload).toContain("ipcRenderer.invoke('inspiration:open-window'");
    expect(preload).toContain("ipcRenderer.on('inspiration:window-state-changed'");
    expect(stickyPreload).toContain("ipcRenderer.send('inspiration:sticky-close')");
    expect(stickyPreload).toContain('取消固定并关闭');
    expect(stickyPreload).toContain('-webkit-app-region: drag');
    expect(stickyPreload).toContain('ace-inspiration-sticky-drag-strip');
    expect(stickyPreload).toContain("gripLabel.textContent = '拖动'");
    expect(stickyPreload).not.toContain('attachShadow');
    expect(build).toContain('inspiration-sticky-preload.ts');
    expect(sitesPage).toContain('onInspirationWindowStateChanged');
    expect(sitesPage).toContain("button.setAttribute('aria-pressed', String(open))");
    expect(schemas).toContain('/^(?:site|canvas)_[0-9a-f]{12}$/i');
  });

  it('mounts generated tool results in a session-scoped annotation surface', async () => {
    const fs = await import('node:fs/promises');
    const [surface, controller, shell, inspector] = await Promise.all([
      fs.readFile('src/ui/features/blueprint-surface.ts', 'utf8'),
      fs.readFile('src/ui/features/chat-controller.ts', 'utf8'),
      fs.readFile('assets/index.html', 'utf8'),
      fs.readFile('src/ui/features/inspector.ts', 'utf8'),
    ]);
    expect(surface).toContain('handleBlueprintSurfaceToolChunk');
    expect(surface).toContain("kind: 'inspiration'");
    expect(surface).toContain("mode: 'widget' | 'canvas'");
    expect(surface).toContain('ace-blueprint-annotation-mode');
    expect(surface).toContain('ace-blueprint-element-selected');
    expect(surface).toContain('data-blueprint-target-note');
    expect(surface).toContain("targetKind: 'canvas'");
    expect(surface).toContain("targetKind: 'widget'");
    expect(surface).toContain("targetKind: 'widget_dom'");
    expect(surface).toContain('resourceRevision');
    expect(surface).toContain('composeBlueprintAnnotationMessage');
    expect(controller).toContain('handleBlueprintSurfaceToolChunk(chunk, sid)');
    expect(controller).toContain('composeBlueprintAnnotationMessage');
    expect(shell).toContain('chat-blueprint-annotation-preview');
    expect(inspector).toContain("'blueprint-surface-open'");
  });
});
