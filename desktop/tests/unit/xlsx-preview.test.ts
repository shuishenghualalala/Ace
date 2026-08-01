/**
 * @vitest-environment happy-dom
 */
import JSZip from 'jszip';
import { describe, expect, it } from 'vitest';

import { loadXlsxPreviewWorkbook } from '../../src/ui/xlsx-preview';

describe('XLSX preview parser', () => {
  it('reads shared strings, number formats, dimensions, and merged cells offline', async () => {
    const zip = new JSZip();
    zip.file('xl/workbook.xml', `<?xml version="1.0"?>
      <workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
        xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
        <sheets><sheet name="数据" sheetId="1" r:id="rId1"/></sheets>
      </workbook>`);
    zip.file('xl/_rels/workbook.xml.rels', `<?xml version="1.0"?>
      <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
        <Relationship Id="rId1" Target="worksheets/sheet1.xml"/>
      </Relationships>`);
    zip.file('xl/sharedStrings.xml', `<?xml version="1.0"?>
      <sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><si><t>项目名称</t></si></sst>`);
    zip.file('xl/styles.xml', `<?xml version="1.0"?>
      <styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
        <numFmts count="1"><numFmt numFmtId="164" formatCode="0.00%"/></numFmts>
        <cellXfs count="2"><xf numFmtId="0"/><xf numFmtId="164"/></cellXfs>
      </styleSheet>`);
    zip.file('xl/worksheets/sheet1.xml', `<?xml version="1.0"?>
      <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
        <cols><col min="1" max="1" width="20"/></cols>
        <sheetData>
          <row r="1" ht="24"><c r="A1" t="s"><v>0</v></c></row>
          <row r="2"><c r="A2" s="1"><v>0.25</v></c><c r="B2"><v>12</v></c></row>
        </sheetData>
        <mergeCells count="1"><mergeCell ref="A1:B1"/></mergeCells>
      </worksheet>`);

    const workbook = await loadXlsxPreviewWorkbook(await zip.generateAsync({ type: 'base64' }));
    expect(workbook.sheetNames).toEqual(['数据']);
    const sheet = await workbook.loadSheet(0);
    expect(sheet.rowCount).toBe(2);
    expect(sheet.columnCount).toBe(2);
    expect(sheet.cells.find((cell) => cell.row === 0 && cell.column === 0)).toMatchObject({
      text: '项目名称',
      columnSpan: 2,
    });
    expect(sheet.cells.find((cell) => cell.row === 1 && cell.column === 0)?.text).toBe('25%');
    expect(sheet.cells.find((cell) => cell.row === 1 && cell.column === 1)?.text).toBe('12');
    expect(sheet.columnWidths[0]).toBeGreaterThan(100);
    expect(sheet.rowHeights.get(0)).toBeGreaterThan(24);
  });
});
