import { EventEmitter } from 'node:events';
import type { WebContents } from 'electron';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('electron', () => ({ shell: { openExternal: vi.fn() }, protocol: {} }));

import { installSitePreviewNavigation } from '../../src/main/site-preview-navigation';

describe('inspiration external links', () => {
  const openExternal = vi.fn<(url: string) => Promise<void>>();
  let contents: EventEmitter & { setWindowOpenHandler: ReturnType<typeof vi.fn<WebContents['setWindowOpenHandler']>> };

  beforeEach(() => {
    openExternal.mockReset().mockResolvedValue(undefined);
    contents = Object.assign(new EventEmitter(), {
      setWindowOpenHandler: vi.fn<WebContents['setWindowOpenHandler']>(),
    });
    installSitePreviewNavigation(contents as unknown as WebContents, openExternal);
  });

  function popup(url: string) {
    const handler = contents.setWindowOpenHandler.mock.calls[0][0];
    return handler({ url } as Parameters<typeof handler>[0]);
  }

  it('opens target=_blank and window.open web URLs in the browser while denying renderer popups', () => {
    expect(popup('https://www.mod.gov.cn/news.html')).toEqual({ action: 'deny' });
    expect(popup('http://example.com/article')).toEqual({ action: 'deny' });
    expect(openExternal.mock.calls).toEqual([
      ['https://www.mod.gov.cn/news.html'], ['http://example.com/article'],
    ]);
  });

  it.each(['file:///etc/passwd', 'javascript:alert(1)', 'data:text/html,test', 'crew://settings', 'not a URL'])(
    'does not pass %s to the OS shell', (url) => {
      expect(popup(url)).toEqual({ action: 'deny' });
      expect(openExternal).not.toHaveBeenCalled();
    },
  );

  it('keeps an inspiration frame in place when opening a same-frame external link', () => {
    const preventDefault = vi.fn();
    contents.emit('will-frame-navigate', {
      url: 'https://example.com/article',
      frame: { url: 'ace-site://widget_0123456789ab/' }, preventDefault,
    });
    expect(preventDefault).toHaveBeenCalledOnce();
    expect(openExternal).toHaveBeenCalledWith('https://example.com/article');
  });

  it('preserves local routes and ignores navigations initiated outside inspiration pages', () => {
    for (const [source, target] of [
      ['ace-site://site_0123456789ab/', 'ace-site://site_0123456789ab/about'],
      ['file:///desktop/index.html', 'https://example.com'],
    ]) {
      const preventDefault = vi.fn();
      contents.emit('will-frame-navigate', { url: target, frame: { url: source }, preventDefault });
      expect(preventDefault).not.toHaveBeenCalled();
    }
    expect(openExternal).not.toHaveBeenCalled();
  });

  it('blocks non-web frame exits and handles browser launch failures', async () => {
    const preventDefault = vi.fn();
    contents.emit('will-frame-navigate', {
      url: 'file:///etc/passwd', frame: { url: 'ace-site://site_0123456789ab/' }, preventDefault,
    });
    expect(preventDefault).toHaveBeenCalledOnce();
    expect(openExternal).not.toHaveBeenCalled();
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined);
    openExternal.mockRejectedValueOnce(new Error('No browser available'));
    popup('https://example.com/');
    await Promise.resolve();
    expect(warn).toHaveBeenCalledOnce();
    warn.mockRestore();
  });
});
