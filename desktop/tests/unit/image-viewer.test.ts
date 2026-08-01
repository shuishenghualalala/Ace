/** @vitest-environment happy-dom */

import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../../src/ui/state', () => ({ notify: vi.fn() }));

import { notify } from '../../src/ui/state';
import {
  closeImageViewer,
  copyImageToClipboard,
  openImageViewer,
  revealImageInFolder,
} from '../../src/ui/features/image-viewer';

describe('image viewer actions', () => {
  const copyImage = vi.fn(async () => ({ ok: true as const }));
  const revealImage = vi.fn(async () => ({ ok: true as const }));

  beforeEach(() => {
    closeImageViewer(false);
    document.body.replaceChildren();
    copyImage.mockClear();
    revealImage.mockClear();
    vi.mocked(notify).mockClear();
    Object.defineProperty(window, 'Crew', {
      configurable: true,
      value: { copyImage, revealImage },
    });
  });

  it('只把本地绝对路径交给主进程复制，并给出成功反馈', async () => {
    expect(await copyImageToClipboard('/tmp/shot.png')).toBe(true);
    expect(copyImage).toHaveBeenCalledWith('/tmp/shot.png');
    expect(notify).toHaveBeenCalledWith('图片已复制');

    expect(await copyImageToClipboard('https://example.test/shot.png')).toBe(false);
    expect(copyImage).toHaveBeenCalledTimes(1);
  });

  it('在文件夹中显示也只接受本地绝对路径', async () => {
    expect(await revealImageInFolder('C:\\Users\\u\\shot.png')).toBe(true);
    expect(revealImage).toHaveBeenCalledWith('C:\\Users\\u\\shot.png');
  });

  it('大图弹层提供复制、定位和关闭，并支持 Escape', () => {
    openImageViewer('crew-file://img/shot', '页面截图', '/tmp/shot.png');
    const viewer = document.querySelector('.chat-image-viewer');
    expect(viewer?.getAttribute('role')).toBe('dialog');
    expect(viewer?.querySelector('.chat-image-viewer__copy')?.textContent).toBe('复制图片');
    expect(viewer?.querySelector('.chat-image-viewer__reveal')?.textContent).toBe('在文件夹中显示');
    expect(document.activeElement?.classList.contains('chat-image-viewer__copy')).toBe(true);

    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    expect(document.querySelector('.chat-image-viewer')).toBeNull();
  });
});
