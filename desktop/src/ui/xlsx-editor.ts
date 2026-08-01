import type { XlsxGridPatch } from './office-edit';
import type { XlsxPreviewSheet } from './xlsx-preview';

const MIN_COLUMN_WIDTH = 48;
const MAX_COLUMN_WIDTH = 420;
const MIN_ROW_HEIGHT = 22;
const MAX_ROW_HEIGHT = 160;

type GridSelection =
  | { kind: 'cell'; row: number; column: number }
  | { kind: 'row'; index: number }
  | { kind: 'column'; index: number }
  | null;

interface XlsxEditorModel extends XlsxGridPatch {
  selection: GridSelection;
}

const editorModels = new WeakMap<HTMLElement, XlsxEditorModel>();

export function columnLabel(index: number): string {
  let value = index + 1;
  let label = '';
  while (value > 0) {
    value -= 1;
    label = String.fromCharCode(65 + (value % 26)) + label;
    value = Math.floor(value / 26);
  }
  return label;
}

function cellName(row: number, column: number): string {
  return `${columnLabel(column)}${row + 1}`;
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, Math.round(value)));
}

function sheetModel(sheet: XlsxPreviewSheet): XlsxEditorModel {
  const rows = Array.from({ length: sheet.rowCount }, () => (
    Array.from({ length: sheet.columnCount }, () => '')
  ));
  const cellStyles = Array.from({ length: sheet.rowCount }, () => (
    Array.from<number | null>({ length: sheet.columnCount }).fill(null)
  ));
  sheet.cells.forEach((cell) => {
    rows[cell.row][cell.column] = cell.formula ?? cell.text;
    cellStyles[cell.row][cell.column] = cell.styleIndex;
  });
  return {
    rows,
    columnWidths: [...sheet.columnWidths],
    rowHeights: Array.from({ length: sheet.rowCount }, (_, row) => sheet.rowHeights.get(row) ?? 28),
    cellStyles,
    structureChanged: false,
    selection: null,
  };
}

function selectedClass(model: XlsxEditorModel, kind: 'row' | 'column', index: number): string {
  return model.selection?.kind === kind && model.selection.index === index ? ' is-selected' : '';
}

function renderEditor(root: HTMLElement, model: XlsxEditorModel): void {
  const selectedCell = model.selection?.kind === 'cell' ? model.selection : null;
  root.innerHTML = `
    <div class="inspector-xlsx-editor__formula-bar">
      <span class="inspector-xlsx-editor__cell-name">${selectedCell ? cellName(selectedCell.row, selectedCell.column) : '—'}</span>
      <span class="inspector-xlsx-editor__fx" aria-hidden="true">fx</span>
      <input type="text" data-xlsx-formula-input aria-label="公式或单元格内容"
        ${selectedCell ? '' : 'disabled'} value="${escapeAttribute(selectedCell ? model.rows[selectedCell.row][selectedCell.column] : '')}"
        placeholder="选择单元格；输入 = 开头的内容可保存公式">
    </div>
    <div class="inspector-xlsx-editor__grid">
      <table class="inspector-xlsx-editor__table">
        <colgroup>
          <col class="inspector-xlsx-editor__row-number-column">
          ${model.columnWidths.map((width, column) => `<col data-xlsx-column-width="${column}" style="width:${width}px">`).join('')}
        </colgroup>
        <thead><tr><th class="inspector-xlsx-editor__corner"></th>${model.columnWidths.map((_, column) => `
          <th data-xlsx-column="${column}" class="${selectedClass(model, 'column', column).trim()}">
            <span>${columnLabel(column)}</span>
            <button type="button" class="inspector-xlsx-editor__insert inspector-xlsx-editor__insert--column"
              data-xlsx-insert-column="${column}" aria-label="在 ${columnLabel(column)} 列右侧新增列" title="在右侧新增列">+</button>
            <span class="inspector-xlsx-editor__resize inspector-xlsx-editor__resize--column"
              data-xlsx-resize-column="${column}" aria-hidden="true"></span>
          </th>`).join('')}</tr></thead>
        <tbody>${model.rows.map((row, rowIndex) => `
          <tr data-xlsx-row="${rowIndex}" style="height:${model.rowHeights[rowIndex]}px">
            <th data-xlsx-row-header="${rowIndex}" class="${selectedClass(model, 'row', rowIndex).trim()}">
              <span>${rowIndex + 1}</span>
              <button type="button" class="inspector-xlsx-editor__insert inspector-xlsx-editor__insert--row"
                data-xlsx-insert-row="${rowIndex}" aria-label="在第 ${rowIndex + 1} 行下方新增行" title="在下方新增行">+</button>
              <span class="inspector-xlsx-editor__resize inspector-xlsx-editor__resize--row"
                data-xlsx-resize-row="${rowIndex}" aria-hidden="true"></span>
            </th>
            ${row.map((value, columnIndex) => `<td contenteditable="true" spellcheck="false"
              class="${value.startsWith('=') ? 'is-formula' : ''}${selectedCell?.row === rowIndex && selectedCell.column === columnIndex ? ' is-selected' : ''}"
              data-xlsx-cell="${rowIndex}:${columnIndex}" aria-label="${cellName(rowIndex, columnIndex)}">${escapeHtml(value)}</td>`).join('')}
          </tr>`).join('')}</tbody>
      </table>
    </div>`;
  bindEditorEvents(root, model);
}

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

function escapeAttribute(value: string): string {
  return escapeHtml(value).replace(/\r?\n/g, '&#10;');
}

function parseIndex(value: string | null): number | null {
  const parsed = Number.parseInt(value ?? '', 10);
  return Number.isFinite(parsed) ? parsed : null;
}

function insertColumn(model: XlsxEditorModel, after: number): void {
  const index = after + 1;
  model.rows.forEach((row) => row.splice(index, 0, ''));
  model.cellStyles.forEach((row) => row.splice(index, 0, row[after] ?? null));
  model.columnWidths.splice(index, 0, model.columnWidths[after] ?? 96);
  model.structureChanged = true;
  model.selection = { kind: 'column', index };
}

function insertRow(model: XlsxEditorModel, after: number): void {
  const index = after + 1;
  const width = model.columnWidths.length;
  model.rows.splice(index, 0, Array.from({ length: width }, () => ''));
  model.cellStyles.splice(index, 0, [...(model.cellStyles[after] ?? Array.from({ length: width }).fill(null))]);
  model.rowHeights.splice(index, 0, model.rowHeights[after] ?? 28);
  model.structureChanged = true;
  model.selection = { kind: 'row', index };
}

function clearSelection(model: XlsxEditorModel): void {
  if (model.selection?.kind === 'row') {
    model.rows[model.selection.index] = model.rows[model.selection.index].map(() => '');
  } else if (model.selection?.kind === 'column') {
    const index = model.selection.index;
    model.rows.forEach((row) => { row[index] = ''; });
  }
}

function deleteSelection(model: XlsxEditorModel): void {
  if (model.selection?.kind === 'row' && model.rows.length > 1) {
    model.rows.splice(model.selection.index, 1);
    model.cellStyles.splice(model.selection.index, 1);
    model.rowHeights.splice(model.selection.index, 1);
    model.structureChanged = true;
    model.selection = null;
  } else if (model.selection?.kind === 'column' && model.columnWidths.length > 1) {
    const index = model.selection.index;
    model.rows.forEach((row) => row.splice(index, 1));
    model.cellStyles.forEach((row) => row.splice(index, 1));
    model.columnWidths.splice(index, 1);
    model.structureChanged = true;
    model.selection = null;
  }
}

function closeContextMenu(root: HTMLElement): void {
  root.querySelector('.inspector-xlsx-editor__context-menu')?.remove();
}

function openContextMenu(root: HTMLElement, model: XlsxEditorModel, x: number, y: number): void {
  closeContextMenu(root);
  if (!model.selection || model.selection.kind === 'cell') return;
  const noun = model.selection.kind === 'row' ? '行' : '列';
  const menu = document.createElement('div');
  menu.className = 'inspector-xlsx-editor__context-menu';
  menu.setAttribute('role', 'menu');
  menu.style.left = `${x}px`;
  menu.style.top = `${y}px`;
  menu.innerHTML = `
    <button type="button" role="menuitem" data-xlsx-clear-selection>清空整${noun}内容</button>
    <button type="button" role="menuitem" class="is-danger" data-xlsx-delete-selection>删除整${noun}</button>`;
  root.appendChild(menu);
  menu.querySelector<HTMLButtonElement>('[data-xlsx-clear-selection]')?.addEventListener('click', () => {
    clearSelection(model);
    renderEditor(root, model);
  });
  menu.querySelector<HTMLButtonElement>('[data-xlsx-delete-selection]')?.addEventListener('click', () => {
    deleteSelection(model);
    renderEditor(root, model);
  });
}

function bindResize(
  root: HTMLElement,
  handle: HTMLElement,
  model: XlsxEditorModel,
  kind: 'row' | 'column',
  index: number,
): void {
  handle.addEventListener('mousedown', (event) => {
    event.preventDefault();
    event.stopPropagation();
    const start = kind === 'column' ? event.clientX : event.clientY;
    const original = kind === 'column' ? model.columnWidths[index] : model.rowHeights[index];
    const move = (moveEvent: MouseEvent): void => {
      const delta = (kind === 'column' ? moveEvent.clientX : moveEvent.clientY) - start;
      if (kind === 'column') {
        model.columnWidths[index] = clamp(original + delta, MIN_COLUMN_WIDTH, MAX_COLUMN_WIDTH);
        const column = root.querySelector<HTMLElement>(`[data-xlsx-column-width="${index}"]`);
        if (column) column.style.width = `${model.columnWidths[index]}px`;
      } else {
        model.rowHeights[index] = clamp(original + delta, MIN_ROW_HEIGHT, MAX_ROW_HEIGHT);
        const row = root.querySelector<HTMLElement>(`[data-xlsx-row="${index}"]`);
        if (row) row.style.height = `${model.rowHeights[index]}px`;
      }
    };
    const end = (): void => {
      window.removeEventListener('mousemove', move);
      window.removeEventListener('mouseup', end);
    };
    window.addEventListener('mousemove', move);
    window.addEventListener('mouseup', end);
  });
}

function bindEditorEvents(root: HTMLElement, model: XlsxEditorModel): void {
  root.querySelectorAll<HTMLButtonElement>('[data-xlsx-insert-column]').forEach((button) => {
    button.addEventListener('click', (event) => {
      event.stopPropagation();
      const index = parseIndex(button.getAttribute('data-xlsx-insert-column'));
      if (index == null) return;
      insertColumn(model, index);
      renderEditor(root, model);
    });
  });
  root.querySelectorAll<HTMLButtonElement>('[data-xlsx-insert-row]').forEach((button) => {
    button.addEventListener('click', (event) => {
      event.stopPropagation();
      const index = parseIndex(button.getAttribute('data-xlsx-insert-row'));
      if (index == null) return;
      insertRow(model, index);
      renderEditor(root, model);
    });
  });
  root.querySelectorAll<HTMLElement>('[data-xlsx-column]').forEach((header) => {
    const index = parseIndex(header.getAttribute('data-xlsx-column'));
    if (index == null) return;
    header.addEventListener('click', () => {
      model.selection = { kind: 'column', index };
      renderEditor(root, model);
    });
    header.addEventListener('contextmenu', (event) => {
      event.preventDefault();
      model.selection = { kind: 'column', index };
      renderEditor(root, model);
      openContextMenu(root, model, event.clientX, event.clientY);
    });
  });
  root.querySelectorAll<HTMLElement>('[data-xlsx-row-header]').forEach((header) => {
    const index = parseIndex(header.getAttribute('data-xlsx-row-header'));
    if (index == null) return;
    header.addEventListener('click', () => {
      model.selection = { kind: 'row', index };
      renderEditor(root, model);
    });
    header.addEventListener('contextmenu', (event) => {
      event.preventDefault();
      model.selection = { kind: 'row', index };
      renderEditor(root, model);
      openContextMenu(root, model, event.clientX, event.clientY);
    });
  });
  root.querySelectorAll<HTMLElement>('[data-xlsx-cell]').forEach((cell) => {
    const [row, column] = (cell.getAttribute('data-xlsx-cell') ?? '').split(':').map(Number);
    if (!Number.isFinite(row) || !Number.isFinite(column)) return;
    cell.addEventListener('focus', () => {
      model.selection = { kind: 'cell', row, column };
      root.querySelector('.inspector-xlsx-editor__cell-name')!.textContent = cellName(row, column);
      const formula = root.querySelector<HTMLInputElement>('[data-xlsx-formula-input]');
      if (formula) {
        formula.disabled = false;
        formula.value = model.rows[row][column];
      }
      root.querySelectorAll('.is-selected').forEach((selected) => selected.classList.remove('is-selected'));
      cell.classList.add('is-selected');
    });
    cell.addEventListener('input', () => {
      model.rows[row][column] = cell.textContent ?? '';
      cell.classList.toggle('is-formula', model.rows[row][column].startsWith('='));
      const formula = root.querySelector<HTMLInputElement>('[data-xlsx-formula-input]');
      if (formula && model.selection?.kind === 'cell' && model.selection.row === row && model.selection.column === column) {
        formula.value = model.rows[row][column];
      }
    });
  });
  const formulaInput = root.querySelector<HTMLInputElement>('[data-xlsx-formula-input]');
  formulaInput?.addEventListener('input', () => {
    if (model.selection?.kind !== 'cell') return;
    const { row, column } = model.selection;
    model.rows[row][column] = formulaInput.value;
    const cell = root.querySelector<HTMLElement>(`[data-xlsx-cell="${row}:${column}"]`);
    if (cell) {
      cell.textContent = formulaInput.value;
      cell.classList.toggle('is-formula', formulaInput.value.startsWith('='));
    }
  });
  root.querySelectorAll<HTMLElement>('[data-xlsx-resize-column]').forEach((handle) => {
    const index = parseIndex(handle.getAttribute('data-xlsx-resize-column'));
    if (index != null) bindResize(root, handle, model, 'column', index);
  });
  root.querySelectorAll<HTMLElement>('[data-xlsx-resize-row]').forEach((handle) => {
    const index = parseIndex(handle.getAttribute('data-xlsx-resize-row'));
    if (index != null) bindResize(root, handle, model, 'row', index);
  });
  if (root.dataset.contextDismissBound !== 'true') {
    root.dataset.contextDismissBound = 'true';
    root.addEventListener('mousedown', (event) => {
      if (!(event.target as Element).closest('.inspector-xlsx-editor__context-menu')) closeContextMenu(root);
    });
  }
}

export function mountXlsxEditor(root: HTMLElement, sheet: XlsxPreviewSheet): void {
  root.className = 'inspector-xlsx-editor';
  const model = sheetModel(sheet);
  editorModels.set(root, model);
  renderEditor(root, model);
}

export function collectXlsxEditorPatch(root: HTMLElement): XlsxGridPatch | null {
  const model = editorModels.get(root);
  if (!model) return null;
  return {
    rows: model.rows.map((row) => [...row]),
    columnWidths: [...model.columnWidths],
    rowHeights: [...model.rowHeights],
    cellStyles: model.cellStyles.map((row) => [...row]),
    structureChanged: model.structureChanged,
  };
}
