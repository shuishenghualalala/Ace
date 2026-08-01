import JSZip from 'jszip';

const MAX_PREVIEW_ROWS = 500;
const MAX_PREVIEW_COLUMNS = 100;

export interface XlsxPreviewCell {
  row: number;
  column: number;
  text: string;
  formula: string | null;
  styleIndex: number | null;
  rowSpan: number;
  columnSpan: number;
}

export interface XlsxPreviewSheet {
  name: string;
  rowCount: number;
  columnCount: number;
  cells: XlsxPreviewCell[];
  columnWidths: number[];
  rowHeights: Map<number, number>;
  truncated: boolean;
}

export interface XlsxPreviewWorkbook {
  sheetNames: string[];
  loadSheet(index: number): Promise<XlsxPreviewSheet>;
}

interface SheetSource {
  name: string;
  path: string;
}

function parseXml(source: string): XMLDocument {
  const xml = new DOMParser().parseFromString(source, 'application/xml');
  if (xml.querySelector('parsererror')) throw new Error('Excel 文件中的 XML 无法解析');
  return xml;
}

function elements(root: Document | Element, localName: string): Element[] {
  const namespaced = Array.from(root.getElementsByTagNameNS?.('*', localName) ?? []);
  return namespaced.length > 0
    ? namespaced as Element[]
    : Array.from(root.getElementsByTagName(localName));
}

function firstElement(root: Document | Element, localName: string): Element | null {
  return elements(root, localName)[0] ?? null;
}

function normalizeZipPath(target: string): string {
  const normalized = target.replace(/\\/g, '/').replace(/^\/+/, '');
  const withRoot = normalized.startsWith('xl/') ? normalized : `xl/${normalized}`;
  const parts: string[] = [];
  for (const part of withRoot.split('/')) {
    if (!part || part === '.') continue;
    if (part === '..') parts.pop();
    else parts.push(part);
  }
  return parts.join('/');
}

function columnIndex(reference: string): number {
  const letters = reference.match(/^[A-Z]+/i)?.[0]?.toUpperCase() ?? 'A';
  let value = 0;
  for (const char of letters) value = value * 26 + char.charCodeAt(0) - 64;
  return Math.max(0, value - 1);
}

function rowIndex(reference: string): number {
  const value = Number.parseInt(reference.match(/\d+$/)?.[0] ?? '1', 10);
  return Math.max(0, value - 1);
}

function cellPosition(reference: string): { row: number; column: number } {
  return { row: rowIndex(reference), column: columnIndex(reference) };
}

function textContentFromRuns(root: Element): string {
  return elements(root, 't').map((node) => node.textContent ?? '').join('');
}

function isDateFormat(formatCode: string): boolean {
  const stripped = formatCode.replace(/"[^"]*"|\[[^\]]*\]|\\./g, '').toLowerCase();
  return /(?:^|[^a-z])[ymdhis]+(?:[^a-z]|$)/.test(stripped);
}

function isPercentFormat(formatCode: string): boolean {
  return formatCode.replace(/"[^"]*"/g, '').includes('%');
}

function excelDate(serial: number, date1904: boolean): string {
  const epoch = Date.UTC(date1904 ? 1904 : 1899, date1904 ? 0 : 11, date1904 ? 1 : 30);
  const date = new Date(epoch + serial * 86_400_000);
  const hasTime = Math.abs(serial % 1) > 1e-8;
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    ...(hasTime ? { hour: '2-digit', minute: '2-digit' } : {}),
    timeZone: 'UTC',
  }).format(date);
}

function buildNumberFormats(stylesXml: string | null): string[] {
  if (!stylesXml) return [];
  const styles = parseXml(stylesXml);
  const custom = new Map<number, string>();
  for (const numFmt of elements(styles, 'numFmt')) {
    const id = Number.parseInt(numFmt.getAttribute('numFmtId') ?? '', 10);
    const code = numFmt.getAttribute('formatCode');
    if (Number.isFinite(id) && code) custom.set(id, code);
  }
  const builtInDates = new Set([14, 15, 16, 17, 18, 19, 20, 21, 22, 27, 30, 36, 45, 46, 47, 50, 57]);
  const cellXfs = firstElement(styles, 'cellXfs');
  if (!cellXfs) return [];
  return elements(cellXfs, 'xf').map((xf) => {
    const id = Number.parseInt(xf.getAttribute('numFmtId') ?? '0', 10);
    if (custom.has(id)) return custom.get(id) ?? '';
    return builtInDates.has(id) ? 'yyyy-mm-dd' : '';
  });
}

function formatCellValue(
  cell: Element,
  sharedStrings: string[],
  numberFormats: string[],
  date1904: boolean,
): string {
  const type = cell.getAttribute('t') ?? '';
  if (type === 'inlineStr') return textContentFromRuns(firstElement(cell, 'is') ?? cell);
  const raw = firstElement(cell, 'v')?.textContent ?? '';
  if (type === 's') return sharedStrings[Number.parseInt(raw, 10)] ?? '';
  if (type === 'b') return raw === '1' ? 'TRUE' : 'FALSE';
  if (type === 'e') return raw ? `#${raw}` : '#ERROR';
  if (type === 'str') return raw;
  if (!raw) {
    const formula = firstElement(cell, 'f')?.textContent;
    return formula ? `=${formula}` : '';
  }
  const numeric = Number(raw);
  if (!Number.isFinite(numeric)) return raw;
  const styleIndex = Number.parseInt(cell.getAttribute('s') ?? '-1', 10);
  const formatCode = numberFormats[styleIndex] ?? '';
  if (formatCode && isDateFormat(formatCode)) return excelDate(numeric, date1904);
  if (formatCode && isPercentFormat(formatCode)) return `${(numeric * 100).toLocaleString('zh-CN')}%`;
  return numeric.toLocaleString('zh-CN', { maximumFractionDigits: 12 });
}

async function optionalText(zip: JSZip, path: string): Promise<string | null> {
  const file = zip.file(path);
  return file ? file.async('text') : null;
}

function mergedRanges(sheet: XMLDocument): Array<{ startRow: number; startColumn: number; endRow: number; endColumn: number }> {
  return elements(sheet, 'mergeCell').flatMap((merge) => {
    const [startRef, endRef] = (merge.getAttribute('ref') ?? '').split(':');
    if (!startRef) return [];
    const start = cellPosition(startRef);
    const end = cellPosition(endRef || startRef);
    return [{ startRow: start.row, startColumn: start.column, endRow: end.row, endColumn: end.column }];
  });
}

async function parseSheet(
  zip: JSZip,
  source: SheetSource,
  sharedStrings: string[],
  numberFormats: string[],
  date1904: boolean,
): Promise<XlsxPreviewSheet> {
  const sourceXml = await optionalText(zip, source.path);
  if (!sourceXml) throw new Error(`Excel 工作表缺失：${source.name}`);
  const sheet = parseXml(sourceXml);
  const cellMap = new Map<string, XlsxPreviewCell>();
  let actualMaxRow = 0;
  let actualMaxColumn = 0;
  for (const cell of elements(sheet, 'c')) {
    const reference = cell.getAttribute('r');
    if (!reference) continue;
    const position = cellPosition(reference);
    actualMaxRow = Math.max(actualMaxRow, position.row + 1);
    actualMaxColumn = Math.max(actualMaxColumn, position.column + 1);
    if (position.row >= MAX_PREVIEW_ROWS || position.column >= MAX_PREVIEW_COLUMNS) continue;
    cellMap.set(`${position.row}:${position.column}`, {
      ...position,
      text: formatCellValue(cell, sharedStrings, numberFormats, date1904),
      formula: firstElement(cell, 'f')?.textContent
        ? `=${firstElement(cell, 'f')?.textContent ?? ''}`
        : null,
      styleIndex: Number.isFinite(Number.parseInt(cell.getAttribute('s') ?? '', 10))
        ? Number.parseInt(cell.getAttribute('s') ?? '', 10)
        : null,
      rowSpan: 1,
      columnSpan: 1,
    });
  }

  const covered = new Set<string>();
  for (const merge of mergedRanges(sheet)) {
    if (merge.startRow >= MAX_PREVIEW_ROWS || merge.startColumn >= MAX_PREVIEW_COLUMNS) continue;
    const endRow = Math.min(merge.endRow, MAX_PREVIEW_ROWS - 1);
    const endColumn = Math.min(merge.endColumn, MAX_PREVIEW_COLUMNS - 1);
    const key = `${merge.startRow}:${merge.startColumn}`;
    const anchor = cellMap.get(key) ?? {
      row: merge.startRow,
      column: merge.startColumn,
      text: '',
      formula: null,
      styleIndex: null,
      rowSpan: 1,
      columnSpan: 1,
    };
    anchor.rowSpan = endRow - merge.startRow + 1;
    anchor.columnSpan = endColumn - merge.startColumn + 1;
    cellMap.set(key, anchor);
    for (let row = merge.startRow; row <= endRow; row += 1) {
      for (let column = merge.startColumn; column <= endColumn; column += 1) {
        if (row !== merge.startRow || column !== merge.startColumn) covered.add(`${row}:${column}`);
      }
    }
  }

  const rowCount = Math.max(1, Math.min(actualMaxRow, MAX_PREVIEW_ROWS));
  const columnCount = Math.max(1, Math.min(actualMaxColumn, MAX_PREVIEW_COLUMNS));
  const cells: XlsxPreviewCell[] = [];
  for (let row = 0; row < rowCount; row += 1) {
    for (let column = 0; column < columnCount; column += 1) {
      const key = `${row}:${column}`;
      if (covered.has(key)) continue;
      cells.push(cellMap.get(key) ?? {
        row,
        column,
        text: '',
        formula: null,
        styleIndex: null,
        rowSpan: 1,
        columnSpan: 1,
      });
    }
  }

  const columnWidths = Array.from({ length: columnCount }, () => 96);
  for (const column of elements(sheet, 'col')) {
    const min = Math.max(1, Number.parseInt(column.getAttribute('min') ?? '1', 10));
    const max = Math.min(columnCount, Number.parseInt(column.getAttribute('max') ?? String(min), 10));
    const width = Number.parseFloat(column.getAttribute('width') ?? '12');
    const pixels = Math.min(420, Math.max(36, Math.round(width * 7.2 + 8)));
    for (let index = min - 1; index < max; index += 1) columnWidths[index] = pixels;
  }
  const rowHeights = new Map<number, number>();
  for (const row of elements(sheet, 'row')) {
    const index = Number.parseInt(row.getAttribute('r') ?? '0', 10) - 1;
    const height = Number.parseFloat(row.getAttribute('ht') ?? '');
    if (index >= 0 && index < rowCount && Number.isFinite(height)) rowHeights.set(index, Math.max(20, height * 1.34));
  }

  return {
    name: source.name,
    rowCount,
    columnCount,
    cells,
    columnWidths,
    rowHeights,
    truncated: actualMaxRow > MAX_PREVIEW_ROWS || actualMaxColumn > MAX_PREVIEW_COLUMNS,
  };
}

export async function loadXlsxPreviewWorkbook(base64: string): Promise<XlsxPreviewWorkbook> {
  const zip = await JSZip.loadAsync(base64, { base64: true });
  const workbookXml = await optionalText(zip, 'xl/workbook.xml');
  const relationshipsXml = await optionalText(zip, 'xl/_rels/workbook.xml.rels');
  if (!workbookXml || !relationshipsXml) throw new Error('不是有效的 XLSX 工作簿');
  const workbook = parseXml(workbookXml);
  const relationships = parseXml(relationshipsXml);
  const targets = new Map<string, string>();
  for (const relationship of elements(relationships, 'Relationship')) {
    const id = relationship.getAttribute('Id');
    const target = relationship.getAttribute('Target');
    if (id && target) targets.set(id, normalizeZipPath(target));
  }
  const sheets: SheetSource[] = elements(workbook, 'sheet').flatMap((sheet) => {
    const id = sheet.getAttribute('r:id')
      ?? sheet.getAttributeNS('http://schemas.openxmlformats.org/officeDocument/2006/relationships', 'id');
    const target = id ? targets.get(id) : undefined;
    return target ? [{ name: sheet.getAttribute('name') || '工作表', path: target }] : [];
  });
  if (sheets.length === 0) throw new Error('Excel 工作簿中没有可预览的工作表');

  const sharedStringsXml = await optionalText(zip, 'xl/sharedStrings.xml');
  const sharedStrings = sharedStringsXml
    ? elements(parseXml(sharedStringsXml), 'si').map(textContentFromRuns)
    : [];
  const numberFormats = buildNumberFormats(await optionalText(zip, 'xl/styles.xml'));
  const date1904 = firstElement(workbook, 'workbookPr')?.getAttribute('date1904') === '1';

  return {
    sheetNames: sheets.map((sheet) => sheet.name),
    loadSheet: async (index: number) => {
      const source = sheets[index];
      if (!source) throw new Error('Excel 工作表索引无效');
      return parseSheet(zip, source, sharedStrings, numberFormats, date1904);
    },
  };
}
