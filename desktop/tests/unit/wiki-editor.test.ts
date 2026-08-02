import { describe, expect, it } from 'vitest';
/**
 * @vitest-environment happy-dom
 */
import { mountWikiEditor, __wikiEditorTest } from '../../src/ui/features/wiki-editor';

describe('Wiki editor Markdown protocol', () => {
  it('turns wikilinks into editable links and restores storage syntax', () => {
    const editable = __wikiEditorTest.markdownForEditor('参考 [[页面 A]] 与 [[页面 B|别名]]。');
    expect(editable).toContain('[页面 A](wiki:%E9%A1%B5%E9%9D%A2%20A)');
    expect(editable).toContain('[别名](wiki:%E9%A1%B5%E9%9D%A2%20B)');
    expect(__wikiEditorTest.markdownForStorage(editable)).toBe('参考 [[页面 A]] 与 [[页面 B|别名]]。');
  });

  it('mounts Markdown as directly editable rich text', () => {
    const element = document.createElement('div');
    document.body.append(element);
    const handle = mountWikiEditor({
      element,
      markdown: '**粗体正文**',
      onChange: () => undefined,
      onWikiLink: () => undefined,
    });
    expect(element.innerHTML).toContain('<strong>粗体正文</strong>');
    expect(element.querySelector('[contenteditable="true"]')).not.toBeNull();
    handle.destroy();
  });

  it('keeps wikilinks in Markdown when rich text is flushed', () => {
    const element = document.createElement('div');
    document.body.append(element);
    const handle = mountWikiEditor({
      element,
      markdown: '参考 [[页面 A]]。',
      onChange: () => undefined,
      onWikiLink: () => undefined,
    });
    expect(element.querySelector('a')?.textContent).toBe('页面 A');
    expect(handle.flush()).toBe('参考 [[页面 A]]。');
    handle.destroy();
  });
});
