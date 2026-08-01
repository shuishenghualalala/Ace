/**
 * @vitest-environment happy-dom
 */
import { describe, expect, it } from 'vitest';
import { buildOfflinePreviewDocument, filePreviewKind } from '../../src/ui/file-preview';

describe('filePreviewKind', () => {
  it.each([
    ['page.html', 'html'],
    ['diagram.SVG', 'svg'],
    ['report.md', 'markdown'],
    ['manual.pdf', 'pdf'],
    ['photo.png', 'image'],
    ['方案.docx', 'docx'],
    ['汇报.PPTX', 'pptx'],
    ['台账.XLSX', 'xlsx'],
    ['旧文档.doc', 'legacy-office'],
    ['旧汇报.ppt', 'legacy-office'],
    ['旧台账.xls', 'legacy-office'],
    ['app.ts', 'code'],
  ])('classifies %s as %s', (path, kind) => {
    expect(filePreviewKind(path)).toBe(kind);
  });
});

describe('buildOfflinePreviewDocument', () => {
  it('blocks network access and keeps local relative resources', () => {
    const html = buildOfflinePreviewDocument(
      '/data/reports/index.html',
      '<html><head><title>x</title></head><body></body></html>',
    );
    expect(html).toContain('connect-src \'none\'');
    expect(html).toContain('form-action \'none\'');
    expect(html).toContain('<base href="file:///data/reports/">');
  });
});
