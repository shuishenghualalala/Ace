/**
 * @vitest-environment happy-dom
 */
import JSZip from 'jszip';
import { describe, expect, it } from 'vitest';

import {
  buildXlsxFromGrid,
  extractXlsxSheet,
  loadPptxEditBlocks,
  patchDocxBlocks,
  patchPptxBlocks,
  patchXlsxGrid,
} from '../../src/ui/office-edit';

function testDocxBase64(text: string): Promise<string> {
  const zip = new JSZip();
  const paragraphs = text.split(/\r?\n/).map((line) => (
    `<w:p><w:r><w:t xml:space="preserve">${line}</w:t></w:r></w:p>`
  )).join('');
  zip.file('word/document.xml', `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>${paragraphs}</w:body></w:document>`);
  return zip.generateAsync({ type: 'base64' });
}

async function docxText(base64: string): Promise<string> {
  const zip = await JSZip.loadAsync(base64, { base64: true });
  return await zip.file('word/document.xml')?.async('text') ?? '';
}

function testPptxBase64(slides: string[][]): Promise<string> {
  const zip = new JSZip();
  slides.forEach((lines, index) => {
    const paragraphs = lines.map((line) => `<a:p><a:r><a:t>${line}</a:t></a:r></a:p>`).join('');
    zip.file(`ppt/slides/slide${index + 1}.xml`, `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"><p:cSld><p:spTree><p:sp><p:txBody>${paragraphs}</p:txBody></p:sp></p:spTree></p:cSld></p:sld>`);
  });
  return zip.generateAsync({ type: 'base64' });
}

async function pptxText(base64: string): Promise<string> {
  const zip = await JSZip.loadAsync(base64, { base64: true });
  const names = Object.keys(zip.files).filter((name) => /^ppt\/slides\/slide\d+\.xml$/i.test(name)).sort();
  const chunks = await Promise.all(names.map(async (name) => await zip.file(name)?.async('text') ?? ''));
  return chunks.join('\n');
}

describe('Office board editors', () => {
  it('patches Word text in the original package instead of rebuilding the file', async () => {
    const original = await JSZip.loadAsync(await testDocxBase64('旧标题'), { base64: true });
    original.file('word/media/keep.txt', 'style-assets');
    const patched = await patchDocxBlocks(await original.generateAsync({ type: 'base64' }), ['新标题']);
    const patchedZip = await JSZip.loadAsync(patched, { base64: true });
    expect(await patchedZip.file('word/media/keep.txt')?.async('text')).toBe('style-assets');
    await expect(docxText(patched)).resolves.toContain('新标题');
  });

  it('loads PPT text blocks used to preserve unedited slide content', async () => {
    const base64 = await testPptxBase64([['第一页', '要点 A'], ['第二页', '要点 B']]);
    const blocks = await loadPptxEditBlocks(base64);
    expect(blocks.map((block) => block.text)).toEqual(['第一页', '要点 A', '第二页', '要点 B']);
  });

  it('patches PPT text in the original package instead of rebuilding the file', async () => {
    const original = await JSZip.loadAsync(await testPptxBase64([['旧页', '旧要点']]), { base64: true });
    original.file('ppt/media/keep.txt', 'theme-assets');
    const patched = await patchPptxBlocks(await original.generateAsync({ type: 'base64' }), ['新页', '新要点']);
    const patchedZip = await JSZip.loadAsync(patched, { base64: true });
    expect(await patchedZip.file('ppt/media/keep.txt')?.async('text')).toBe('theme-assets');
    const text = await pptxText(patched);
    expect(text).toContain('新页');
    expect(text).toContain('新要点');
  });

  it('round-trips Excel grid cells', async () => {
    const base64 = await buildXlsxFromGrid([
      ['姓名', '分数'],
      ['张三', '98'],
    ]);
    const sheet = await extractXlsxSheet(base64);
    expect(sheet.cells.find((cell) => cell.row === 0 && cell.column === 0)?.text).toBe('姓名');
    expect(sheet.cells.find((cell) => cell.row === 1 && cell.column === 1)?.text).toBe('98');
  });

  it('patches Excel cells in the original package instead of rebuilding the workbook', async () => {
    const original = await JSZip.loadAsync(await buildXlsxFromGrid([['旧值']]), { base64: true });
    original.file('xl/theme/theme1.xml', '<theme/>');
    const patched = await patchXlsxGrid(await original.generateAsync({ type: 'base64' }), [['新值']]);
    const patchedZip = await JSZip.loadAsync(patched, { base64: true });
    expect(await patchedZip.file('xl/theme/theme1.xml')?.async('text')).toBe('<theme/>');
    const sheet = await extractXlsxSheet(patched);
    expect(sheet.cells.find((cell) => cell.row === 0 && cell.column === 0)?.text).toBe('新值');
  });

  it('writes Excel formulas and edited row and column dimensions', async () => {
    const patched = await patchXlsxGrid(await buildXlsxFromGrid([['1']]), {
      rows: [['2', '3', '=SUM(A1:B1)']],
      columnWidths: [88, 120, 150],
      rowHeights: [42],
      cellStyles: [[null, null, null]],
      structureChanged: true,
    });
    const zip = await JSZip.loadAsync(patched, { base64: true });
    const xml = await zip.file('xl/worksheets/sheet1.xml')?.async('text') ?? '';
    expect(xml).toContain('<f>SUM(A1:B1)</f>');
    expect(xml).toContain('customWidth="1"');
    expect(xml).toContain('customHeight="1"');

    const sheet = await extractXlsxSheet(patched);
    expect(sheet.columnCount).toBe(3);
    expect(sheet.cells.find((cell) => cell.column === 2)?.formula).toBe('=SUM(A1:B1)');
    expect(sheet.rowHeights.get(0)).toBeCloseTo(42);
  });
});
