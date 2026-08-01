/**
 * @vitest-environment happy-dom
 */
import { beforeEach, describe, expect, it } from 'vitest';

import { collectXlsxEditorPatch, mountXlsxEditor } from '../../src/ui/xlsx-editor';
import type { XlsxPreviewSheet } from '../../src/ui/xlsx-preview';

function sheet(): XlsxPreviewSheet {
  return {
    name: 'Sheet1',
    rowCount: 2,
    columnCount: 2,
    cells: [
      { row: 0, column: 0, text: '2', formula: null, styleIndex: 1, rowSpan: 1, columnSpan: 1 },
      { row: 0, column: 1, text: '3', formula: null, styleIndex: null, rowSpan: 1, columnSpan: 1 },
      { row: 1, column: 0, text: '5', formula: '=SUM(A1:B1)', styleIndex: 2, rowSpan: 1, columnSpan: 1 },
      { row: 1, column: 1, text: '', formula: null, styleIndex: null, rowSpan: 1, columnSpan: 1 },
    ],
    columnWidths: [96, 110],
    rowHeights: new Map([[1, 36]]),
    truncated: false,
  };
}

describe('Excel grid editor', () => {
  beforeEach(() => {
    document.body.innerHTML = '<div id="editor"></div>';
  });

  it('inserts columns to the right and rows below while retaining formulas', () => {
    const root = document.getElementById('editor') as HTMLElement;
    mountXlsxEditor(root, sheet());

    expect(root.querySelector('[data-xlsx-cell="1:0"]')?.textContent).toBe('=SUM(A1:B1)');
    root.querySelector<HTMLButtonElement>('[data-xlsx-insert-column="0"]')?.click();
    root.querySelector<HTMLButtonElement>('[data-xlsx-insert-row="0"]')?.click();

    const patch = collectXlsxEditorPatch(root);
    expect(patch?.rows).toHaveLength(3);
    expect(patch?.rows[0]).toHaveLength(3);
    expect(patch?.rows[2][0]).toBe('=SUM(A1:B1)');
    expect(patch?.structureChanged).toBe(true);
  });

  it('edits formulas through the formula bar', () => {
    const root = document.getElementById('editor') as HTMLElement;
    mountXlsxEditor(root, sheet());
    const cell = root.querySelector<HTMLElement>('[data-xlsx-cell="0:1"]');
    cell?.dispatchEvent(new FocusEvent('focus'));
    const formula = root.querySelector<HTMLInputElement>('[data-xlsx-formula-input]');
    if (!formula) throw new Error('没有公式输入框');
    formula.value = '=A1*10';
    formula.dispatchEvent(new InputEvent('input', { bubbles: true }));

    expect(cell?.textContent).toBe('=A1*10');
    expect(cell?.classList.contains('is-formula')).toBe(true);
    expect(collectXlsxEditorPatch(root)?.rows[0][1]).toBe('=A1*10');
  });

  it('clears or deletes a selected whole row or column from the context menu', () => {
    const root = document.getElementById('editor') as HTMLElement;
    mountXlsxEditor(root, sheet());
    root.querySelector<HTMLElement>('[data-xlsx-row-header="0"]')?.dispatchEvent(new MouseEvent('contextmenu', {
      bubbles: true,
      clientX: 30,
      clientY: 40,
    }));
    root.querySelector<HTMLButtonElement>('[data-xlsx-clear-selection]')?.click();
    expect(collectXlsxEditorPatch(root)?.rows[0]).toEqual(['', '']);

    root.querySelector<HTMLElement>('[data-xlsx-column="1"]')?.dispatchEvent(new MouseEvent('contextmenu', {
      bubbles: true,
      clientX: 50,
      clientY: 60,
    }));
    root.querySelector<HTMLButtonElement>('[data-xlsx-delete-selection]')?.click();
    const patch = collectXlsxEditorPatch(root);
    expect(patch?.columnWidths).toHaveLength(1);
    expect(patch?.rows.every((row) => row.length === 1)).toBe(true);
  });

  it('resizes a column and a row by dragging their boundary handles', () => {
    const root = document.getElementById('editor') as HTMLElement;
    mountXlsxEditor(root, sheet());
    root.querySelector<HTMLElement>('[data-xlsx-resize-column="0"]')?.dispatchEvent(new MouseEvent('mousedown', {
      bubbles: true,
      clientX: 100,
    }));
    window.dispatchEvent(new MouseEvent('mousemove', { clientX: 140 }));
    window.dispatchEvent(new MouseEvent('mouseup'));

    root.querySelector<HTMLElement>('[data-xlsx-resize-row="0"]')?.dispatchEvent(new MouseEvent('mousedown', {
      bubbles: true,
      clientY: 50,
    }));
    window.dispatchEvent(new MouseEvent('mousemove', { clientY: 70 }));
    window.dispatchEvent(new MouseEvent('mouseup'));

    const patch = collectXlsxEditorPatch(root);
    expect(patch?.columnWidths[0]).toBe(136);
    expect(patch?.rowHeights[0]).toBe(48);
  });
});
