/** Route inspiration links to the system browser without creating renderer windows. */
import { shell, type WebContents } from 'electron';
import { parseSitePreviewUrl } from './site-preview-protocol';

export function installSitePreviewNavigation(
  contents: WebContents,
  openExternal: (url: string) => Promise<unknown> = (url) => shell.openExternal(url),
): void {
  const openWebLink = (rawUrl: string): void => {
    try {
      const url = new URL(rawUrl);
      if (url.protocol !== 'https:' && url.protocol !== 'http:') return;
      void openExternal(url.href).catch((error: unknown) => {
        console.warn('[ace-site] Failed to open external link', error);
      });
    } catch {
      // Invalid links and non-web protocols must never reach the OS shell.
    }
  };

  contents.setWindowOpenHandler(({ url }) => {
    openWebLink(url);
    return { action: 'deny' };
  });
  contents.on('will-frame-navigate', (event) => {
    const source = event.initiator?.url || event.frame?.url || '';
    if (!parseSitePreviewUrl(source) || parseSitePreviewUrl(event.url)) return;
    event.preventDefault();
    openWebLink(event.url);
  });
}
