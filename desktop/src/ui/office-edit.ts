import JSZip from 'jszip';

import { loadXlsxPreviewWorkbook, type XlsxPreviewSheet } from './xlsx-preview';

export interface OfficeTextBlock {
  label: string;
  text: string;
}

export interface XlsxGridPatch {
  rows: string[][];
  columnWidths: number[];
  rowHeights: number[];
  cellStyles: Array<Array<number | null>>;
  structureChanged: boolean;
}

function parseXml(source: string): XMLDocument {
  const xml = new DOMParser().parseFromString(source, 'application/xml');
  if (xml.querySelector('parsererror')) throw new Error('Office 文件 XML 无法解析');
  return xml;
}

function elements(root: Document | Element, localName: string): Element[] {
  const all = Array.from(root.getElementsByTagName('*'));
  return all.filter((node) => node.localName === localName || node.tagName.split(':').pop() === localName);
}

function xmlEscape(value: string): string {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function serializeXml(xml: XMLDocument): string {
  return new XMLSerializer().serializeToString(xml);
}

function xmlElement(xml: XMLDocument, name: string): Element {
  return xml.createElementNS(xml.documentElement.namespaceURI, name);
}

function elementText(element: Element): string {
  return elements(element, 't').map((node) => node.textContent ?? '').join('');
}

function replaceTextRuns(element: Element, nextText: string): void {
  const textNodes = elements(element, 't');
  if (textNodes.length === 0) return;
  const current = textNodes.map((node) => node.textContent ?? '');
  if (current.join('') === nextText) return;
  let offset = 0;
  textNodes.forEach((node, index) => {
    const remaining = nextText.slice(offset);
    const length = index === textNodes.length - 1 ? remaining.length : current[index].length;
    node.textContent = remaining.slice(0, length);
    if (node.textContent) node.setAttribute('xml:space', 'preserve');
    offset += length;
  });
}

async function zipText(zip: JSZip, path: string): Promise<string | null> {
  const file = zip.file(path);
  return file ? file.async('text') : null;
}

function addPackageRels(zip: JSZip, officeDocumentTarget: string): void {
  zip.file('_rels/.rels', `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="${officeDocumentTarget}"/>
</Relationships>`);
  zip.file('docProps/core.xml', `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/"><dc:creator>Crew</dc:creator><dc:title>Crew 编辑文档</dc:title><dcterms:created xsi:type="dcterms:W3CDTF" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">${new Date().toISOString()}</dcterms:created></cp:coreProperties>`);
  zip.file('docProps/app.xml', `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"><Application>Crew</Application></Properties>`);
}

export async function patchDocxBlocks(base64: string, blocks: string[]): Promise<string> {
  const zip = await JSZip.loadAsync(base64, { base64: true });
  const xml = await zipText(zip, 'word/document.xml');
  if (!xml) throw new Error('Word 文档缺少正文');
  const documentXml = parseXml(xml);
  const paragraphs = elements(documentXml, 'p').filter((paragraph) => elementText(paragraph).trim().length > 0);
  blocks.forEach((text, index) => {
    const paragraph = paragraphs[index];
    if (paragraph) replaceTextRuns(paragraph, text);
  });
  zip.file('word/document.xml', serializeXml(documentXml));
  return zip.generateAsync({ type: 'base64' });
}

function slidePaths(zip: JSZip): string[] {
  return Object.keys(zip.files)
    .filter((name) => /^ppt\/slides\/slide\d+\.xml$/i.test(name))
    .sort((a, b) => Number(a.match(/slide(\d+)/i)?.[1] ?? 0) - Number(b.match(/slide(\d+)/i)?.[1] ?? 0));
}

export async function loadPptxEditBlocks(base64: string): Promise<OfficeTextBlock[]> {
  const zip = await JSZip.loadAsync(base64, { base64: true });
  const slides = slidePaths(zip);
  if (slides.length === 0) throw new Error('PPT 中没有可编辑幻灯片');
  const blocks: OfficeTextBlock[] = [];
  for (let slideIndex = 0; slideIndex < slides.length; slideIndex += 1) {
    const xml = await zipText(zip, slides[slideIndex]);
    if (!xml) continue;
    const slideXml = parseXml(xml);
    elements(slideXml, 'p').forEach((paragraph, paragraphIndex) => {
      const text = elementText(paragraph);
      if (text.trim()) blocks.push({
        label: `幻灯片 ${slideIndex + 1} · 文本 ${paragraphIndex + 1}`,
        text,
      });
    });
  }
  return blocks;
}

export async function patchPptxBlocks(base64: string, blocks: string[]): Promise<string> {
  const zip = await JSZip.loadAsync(base64, { base64: true });
  const slides = slidePaths(zip);
  let blockIndex = 0;
  for (const slide of slides) {
    const xml = await zipText(zip, slide);
    if (!xml) continue;
    const slideXml = parseXml(xml);
    const paragraphs = elements(slideXml, 'p').filter((paragraph) => elementText(paragraph).trim().length > 0);
    for (const paragraph of paragraphs) {
      const next = blocks[blockIndex];
      if (next !== undefined) replaceTextRuns(paragraph, next);
      blockIndex += 1;
    }
    zip.file(slide, serializeXml(slideXml));
  }
  return zip.generateAsync({ type: 'base64' });
}

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

export async function extractXlsxSheet(base64: string): Promise<XlsxPreviewSheet> {
  const workbook = await loadXlsxPreviewWorkbook(base64);
  return workbook.loadSheet(0);
}

function columnIndexToReference(index: number): string {
  return columnLabel(index);
}

function cellRef(row: number, column: number): string {
  return `${columnIndexToReference(column)}${row + 1}`;
}

function firstElement(root: Document | Element, localName: string): Element | null {
  return elements(root, localName)[0] ?? null;
}

function normalizeZipPath(target: string): string {
  const normalized = target.replace(/\\/g, '/').replace(/^\/+/, '');
  return normalized.startsWith('xl/') ? normalized : `xl/${normalized}`;
}

async function firstWorksheetPath(zip: JSZip): Promise<string> {
  const workbookXml = await zipText(zip, 'xl/workbook.xml');
  const relsXml = await zipText(zip, 'xl/_rels/workbook.xml.rels');
  if (!workbookXml || !relsXml) throw new Error('Excel 工作簿结构不完整');
  const workbook = parseXml(workbookXml);
  const rels = parseXml(relsXml);
  const sheet = firstElement(workbook, 'sheet');
  const id = sheet?.getAttribute('r:id')
    ?? sheet?.getAttributeNS('http://schemas.openxmlformats.org/officeDocument/2006/relationships', 'id');
  if (!id) throw new Error('Excel 工作表关系缺失');
  const relationship = elements(rels, 'Relationship').find((item) => item.getAttribute('Id') === id);
  const target = relationship?.getAttribute('Target');
  if (!target) throw new Error('Excel 工作表文件缺失');
  return normalizeZipPath(target);
}

function findOrCreateSheetData(sheetXml: XMLDocument): Element {
  const existing = firstElement(sheetXml, 'sheetData');
  if (existing) return existing;
  const worksheet = firstElement(sheetXml, 'worksheet') ?? sheetXml.documentElement;
  const created = xmlElement(sheetXml, 'sheetData');
  worksheet.appendChild(created);
  return created;
}

function findOrCreateRow(sheetXml: XMLDocument, sheetData: Element, rowIndex: number): Element {
  const rowNumber = String(rowIndex + 1);
  const existing = elements(sheetData, 'row').find((row) => row.getAttribute('r') === rowNumber);
  if (existing) return existing;
  const row = xmlElement(sheetXml, 'row');
  row.setAttribute('r', rowNumber);
  sheetData.appendChild(row);
  return row;
}

function setCellValue(
  sheetXml: XMLDocument,
  row: Element,
  rowIndex: number,
  columnIndex: number,
  value: string,
  styleIndex: number | null,
): void {
  const ref = cellRef(rowIndex, columnIndex);
  const cell = xmlElement(sheetXml, 'c');
  cell.setAttribute('r', ref);
  if (styleIndex != null) cell.setAttribute('s', String(styleIndex));
  row.appendChild(cell);
  if (value.startsWith('=') && value.length > 1) {
    const formula = xmlElement(sheetXml, 'f');
    formula.textContent = value.slice(1);
    cell.appendChild(formula);
    return;
  }
  cell.setAttribute('t', 'inlineStr');
  const inline = xmlElement(sheetXml, 'is');
  const text = xmlElement(sheetXml, 't');
  text.setAttribute('xml:space', 'preserve');
  text.textContent = value;
  inline.appendChild(text);
  cell.appendChild(inline);
}

function normalizeXlsxPatch(input: string[][] | XlsxGridPatch): XlsxGridPatch {
  if (!Array.isArray(input)) return input;
  const columnCount = Math.max(1, ...input.map((row) => row.length));
  return {
    rows: input.map((row) => Array.from({ length: columnCount }, (_, column) => row[column] ?? '')),
    columnWidths: Array.from({ length: columnCount }, () => 96),
    rowHeights: Array.from({ length: Math.max(1, input.length) }, () => 28),
    cellStyles: Array.from({ length: Math.max(1, input.length) }, () => (
      Array.from<number | null>({ length: columnCount }).fill(null)
    )),
    structureChanged: false,
  };
}

function replaceColumnWidths(sheetXml: XMLDocument, sheetData: Element, widths: number[]): void {
  elements(sheetXml, 'cols').forEach((node) => node.remove());
  const cols = xmlElement(sheetXml, 'cols');
  widths.forEach((pixels, index) => {
    const column = xmlElement(sheetXml, 'col');
    column.setAttribute('min', String(index + 1));
    column.setAttribute('max', String(index + 1));
    column.setAttribute('width', String(Math.max(1, (pixels - 8) / 7.2)));
    column.setAttribute('customWidth', '1');
    cols.appendChild(column);
  });
  sheetData.parentElement?.insertBefore(cols, sheetData);
}

function updateSheetDimension(sheetXml: XMLDocument, rows: string[][]): void {
  const worksheet = firstElement(sheetXml, 'worksheet') ?? sheetXml.documentElement;
  let dimension = firstElement(sheetXml, 'dimension');
  if (!dimension) {
    dimension = xmlElement(sheetXml, 'dimension');
    worksheet.insertBefore(dimension, worksheet.firstChild);
  }
  const lastRow = Math.max(1, rows.length);
  const lastColumn = Math.max(0, ...rows.map((row) => row.length - 1));
  dimension.setAttribute('ref', `A1:${cellRef(lastRow - 1, lastColumn)}`);
}

function markWorkbookForFormulaRecalculation(
  zip: JSZip,
  workbookXml: string,
  workbookRelsXml: string | null,
  contentTypesXml: string | null,
): void {
  const workbook = parseXml(workbookXml);
  let calcPr = firstElement(workbook, 'calcPr');
  if (!calcPr) {
    calcPr = xmlElement(workbook, 'calcPr');
    workbook.documentElement.appendChild(calcPr);
  }
  calcPr.setAttribute('calcMode', 'auto');
  calcPr.setAttribute('fullCalcOnLoad', '1');
  calcPr.setAttribute('forceFullCalc', '1');
  zip.file('xl/workbook.xml', serializeXml(workbook));
  zip.remove('xl/calcChain.xml');
  if (workbookRelsXml) {
    const relationships = parseXml(workbookRelsXml);
    elements(relationships, 'Relationship')
      .filter((relationship) => relationship.getAttribute('Type')?.endsWith('/calcChain'))
      .forEach((relationship) => relationship.remove());
    zip.file('xl/_rels/workbook.xml.rels', serializeXml(relationships));
  }
  if (contentTypesXml) {
    const contentTypes = parseXml(contentTypesXml);
    elements(contentTypes, 'Override')
      .filter((override) => override.getAttribute('PartName') === '/xl/calcChain.xml')
      .forEach((override) => override.remove());
    zip.file('[Content_Types].xml', serializeXml(contentTypes));
  }
}

export async function patchXlsxGrid(base64: string, input: string[][] | XlsxGridPatch): Promise<string> {
  const patch = normalizeXlsxPatch(input);
  const zip = await JSZip.loadAsync(base64, { base64: true });
  const sheetPath = await firstWorksheetPath(zip);
  const xml = await zipText(zip, sheetPath);
  if (!xml) throw new Error('Excel 工作表缺失');
  const sheetXml = parseXml(xml);
  const sheetData = findOrCreateSheetData(sheetXml);
  sheetData.replaceChildren();
  replaceColumnWidths(sheetXml, sheetData, patch.columnWidths);
  patch.rows.forEach((rowValues, rowIndex) => {
    const row = findOrCreateRow(sheetXml, sheetData, rowIndex);
    const height = patch.rowHeights[rowIndex];
    if (Number.isFinite(height)) {
      row.setAttribute('ht', String(Math.max(15, height / 1.34)));
      row.setAttribute('customHeight', '1');
    }
    rowValues.forEach((value, columnIndex) => {
      const styleIndex = patch.cellStyles[rowIndex]?.[columnIndex] ?? null;
      if (value || styleIndex != null) setCellValue(sheetXml, row, rowIndex, columnIndex, value, styleIndex);
    });
  });
  updateSheetDimension(sheetXml, patch.rows);
  if (patch.structureChanged) elements(sheetXml, 'mergeCells').forEach((node) => node.remove());
  zip.file(sheetPath, serializeXml(sheetXml));
  const workbookXml = await zipText(zip, 'xl/workbook.xml');
  if (workbookXml) {
    markWorkbookForFormulaRecalculation(
      zip,
      workbookXml,
      await zipText(zip, 'xl/_rels/workbook.xml.rels'),
      await zipText(zip, '[Content_Types].xml'),
    );
  }
  return zip.generateAsync({ type: 'base64' });
}

export async function buildXlsxFromGrid(rows: string[][]): Promise<string> {
  const zip = new JSZip();
  addPackageRels(zip, 'xl/workbook.xml');
  zip.file('[Content_Types].xml', `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/><Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/></Types>`);
  zip.file('xl/_rels/workbook.xml.rels', `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>`);
  zip.file('xl/workbook.xml', `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>`);
  const sheetRows = rows.map((row, rowIndex) => {
    const cells = row.map((value, columnIndex) => {
      const ref = `${columnLabel(columnIndex)}${rowIndex + 1}`;
      return `<c r="${ref}" t="inlineStr"><is><t>${xmlEscape(value)}</t></is></c>`;
    }).join('');
    return `<row r="${rowIndex + 1}">${cells}</row>`;
  }).join('');
  zip.file('xl/worksheets/sheet1.xml', `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>${sheetRows}</sheetData></worksheet>`);
  return zip.generateAsync({ type: 'base64' });
}
