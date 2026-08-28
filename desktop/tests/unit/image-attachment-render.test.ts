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

  it('文件附件复用主对话文件卡，并携带工作空间范围的预览与下载动作', () => {
    const path = 'C:\\Users\\ahuamao\\project\\report.html';
    const root = renderMessageHtml({
      id: 'm2',
      role: 'user',
      content: '',
      timestamp: 1,
      attachments: [{
        id: 'a2',
        name: 'report.html',
        path,
        type: 'file',
        workspaceId: 'workspace-win',
      }],
    }, '');

    expect(root.querySelector('.msg__attachment--file')?.textContent).toContain('report.html');
    expect(root.querySelector('[data-attachment-preview]')?.getAttribute('data-attachment-preview')).toBe(path);
    expect(root.querySelector('[data-attachment-preview]')?.getAttribute('data-attachment-workspace')).toBe('workspace-win');
    expect(root.querySelector('[data-attachment-download]')?.getAttribute('data-attachment-download')).toBe(path);
    expect(root.querySelector('[data-attachment-download]')?.getAttribute('data-attachment-workspace')).toBe('workspace-win');
  });
});
