/** @vitest-environment happy-dom */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { backendApi } from '../../src/ui/backend-client';
import {
  bindAttachments,
  bindFileDrop,
  bindFilePaste,
  uniqueClipboardFiles,
} from '../../src/ui/features/attachments';
import { __resetAllStoresForTest } from '../../src/ui/stores/stores';

function clipboardSource(files: File[], itemFiles: File[]): Pick<DataTransfer, 'files' | 'items'> {
  return {
    files: files as unknown as FileList,
    items: itemFiles.map((file) => ({
      kind: 'file',
      type: file.type,
      getAsFile: () => file,
    })) as unknown as DataTransferItemList,
  };
}

describe('uniqueClipboardFiles', () => {
  it('uses files as the canonical clipboard view even when item wrappers differ', () => {
    const fromFiles = new File(['same image'], 'image.png', {
      type: 'image/png',
      lastModified: 123,
    });
    const fromItems = new File(['same image'], 'image.png', {
      type: 'image/png',
      lastModified: 456,
    });

    expect(uniqueClipboardFiles(clipboardSource([fromFiles], [fromItems]))).toEqual([fromFiles]);
  });

  it('does not merge item entries into a non-empty files list', () => {
    const canonical = new File(['one'], 'one.png', { type: 'image/png', lastModified: 1 });
    const secondView = new File(['two'], 'two.png', { type: 'image/png', lastModified: 2 });

    expect(uniqueClipboardFiles(clipboardSource([canonical], [secondView]))).toEqual([canonical]);
  });

  it('falls back to item files when the canonical list is empty', () => {
    const first = new File(['one'], 'one.png', { type: 'image/png', lastModified: 1 });
    const second = new File(['two'], 'two.png', { type: 'image/png', lastModified: 2 });

    expect(uniqueClipboardFiles(clipboardSource([], [first, second]))).toEqual([first, second]);
  });

  it('deduplicates generated item wrappers without trusting lastModified', () => {
    const first = new File(['same image'], 'image.png', {
      type: 'image/png',
      lastModified: 1,
    });
    const duplicate = new File(['same image'], 'image.png', {
      type: 'image/png',
      lastModified: 2,
    });

    expect(uniqueClipboardFiles(clipboardSource([], [first, duplicate]))).toEqual([first]);
  });
});

describe('clipboard attachment binding', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    __resetAllStoresForTest();
    document.body.innerHTML = `
      <div class="chat-input-container">
        <textarea data-composer-input></textarea>
      </div>
      <div data-attachment-preview></div>
    `;
  });

  it('uploads one attachment when one bitmap appears in files and items', async () => {
    const fromFiles = new File(['same image'], 'image.png', {
      type: 'image/png',
      lastModified: 123,
    });
    const fromItems = new File(['same image'], 'image.png', {
      type: 'image/png',
      lastModified: 456,
    });
    const upload = vi.spyOn(backendApi, 'upload').mockResolvedValue({
      id: 'att-1',
      name: 'image.png',
      path: '/tmp/image.png',
      type: 'image',
      size: fromFiles.size,
    });
    bindAttachments();
    bindAttachments();

    const paste = new Event('paste', { bubbles: true, cancelable: true });
    Object.defineProperty(paste, 'clipboardData', {
      value: clipboardSource([fromFiles], [fromItems]),
    });
    document.querySelector('[data-composer-input]')?.dispatchEvent(paste);

    await vi.waitFor(() => expect(upload).toHaveBeenCalledOnce());
    expect(upload).toHaveBeenCalledWith('image.png', expect.any(String));
    expect(paste.defaultPrevented).toBe(true);
  });
});

/**
 * 通用粘贴/拖拽绑定（bindFilePaste / bindFileDrop）：主对话与 Wiki 右栏 Composer 共用，
 * 各自传入上传回调。这里直接用回调断言，不走全局附件状态。
 */
describe('generic file paste/drop binders', () => {
  beforeEach(() => {
    document.body.innerHTML = '';
  });

  function pasteEvent(data: Pick<DataTransfer, 'files' | 'items'> | null): Event {
    const event = new Event('paste', { bubbles: true, cancelable: true });
    Object.defineProperty(event, 'clipboardData', { value: data });
    return event;
  }

  function dragEvent(type: string, data: Partial<DataTransfer>): Event {
    const event = new Event(type, { bubbles: true, cancelable: true });
    Object.defineProperty(event, 'dataTransfer', { value: data });
    return event;
  }

  it('bindFilePaste delivers clipboard files and prevents default', () => {
    const target = document.createElement('textarea');
    const onFiles = vi.fn();
    bindFilePaste(target, onFiles);

    const file = new File(['img'], 'shot.png', { type: 'image/png' });
    const event = pasteEvent(clipboardSource([file], []));
    target.dispatchEvent(event);

    expect(onFiles).toHaveBeenCalledWith([file]);
    expect(event.defaultPrevented).toBe(true);
  });

  it('bindFilePaste ignores text-only paste and binds only once per element', () => {
    const target = document.createElement('textarea');
    const onFiles = vi.fn();
    bindFilePaste(target, onFiles);
    bindFilePaste(target, onFiles);

    const empty = pasteEvent(clipboardSource([], []));
    target.dispatchEvent(empty);
    expect(onFiles).not.toHaveBeenCalled();
    expect(empty.defaultPrevented).toBe(false);

    const file = new File(['img'], 'shot.png', { type: 'image/png' });
    target.dispatchEvent(pasteEvent(clipboardSource([file], [])));
    expect(onFiles).toHaveBeenCalledOnce();
  });

  it('bindFileDrop delivers dropped files and manages the drag-over class', () => {
    const zone = document.createElement('div');
    const onFiles = vi.fn();
    bindFileDrop(zone, onFiles);

    const file = new File(['doc'], 'a.pdf', { type: 'application/pdf' });
    const fileDrag = {
      types: ['Files'],
      files: [file] as unknown as FileList,
      dropEffect: '',
    } as Partial<DataTransfer>;

    const over = dragEvent('dragover', fileDrag);
    zone.dispatchEvent(over);
    expect(over.defaultPrevented).toBe(true);

    zone.dispatchEvent(dragEvent('dragenter', fileDrag));
    expect(zone.classList.contains('is-drag-over')).toBe(true);

    const drop = dragEvent('drop', fileDrag);
    zone.dispatchEvent(drop);
    expect(drop.defaultPrevented).toBe(true);
    expect(onFiles).toHaveBeenCalledWith([file]);
    expect(zone.classList.contains('is-drag-over')).toBe(false);
  });

  it('bindFileDrop ignores non-file drags', () => {
    const zone = document.createElement('div');
    const onFiles = vi.fn();
    bindFileDrop(zone, onFiles);

    const textDrag = { types: ['text/plain'], files: [] as unknown as FileList } as Partial<DataTransfer>;
    const over = dragEvent('dragover', textDrag);
    zone.dispatchEvent(over);
    zone.dispatchEvent(dragEvent('drop', textDrag));

    expect(over.defaultPrevented).toBe(false);
    expect(onFiles).not.toHaveBeenCalled();
    expect(zone.classList.contains('is-drag-over')).toBe(false);
  });
});
