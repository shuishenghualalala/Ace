/** @vitest-environment happy-dom */

import { describe, expect, it } from 'vitest';
import { renderMessageHtml } from '../../src/ui/chat-render';
import { crewFileUrl } from '../../src/ui/tool-screenshot';

describe('chat image attachment rendering', () => {
  it('本地图片附件走账号私有协议，并提供查看/复制/定位操作', () => {
    const path = '/home/u/.Crew/accounts/acct_0123456789abcdef/uploads/photo.png';
    const root = renderMessageHtml({
      id: 'm1',
      role: 'user',
      content: '请看附件',
      timestamp: 1,
      attachments: [{ id: 'a1', name: 'photo.png', path, type: 'image' }],
    }, '');

    expect(root.querySelector<HTMLImageElement>('.msg__attachment-image')?.src).toBe(crewFileUrl(path));
    expect(root.querySelector('[data-image-view-src]')?.getAttribute('data-image-local-path')).toBe(path);
    expect(root.querySelector('[data-image-copy-path]')?.getAttribute('data-image-copy-path')).toBe(path);
    expect(root.querySelector('[data-image-reveal-path]')?.getAttribute('data-image-reveal-path')).toBe(path);
    expect(root.querySelector('[data-image-copy-path]')?.getAttribute('aria-label')).toBe('复制图片');
    expect(root.querySelector('[data-image-copy-path]')?.textContent).toBe('');
  });
});
