/**
 * @vitest-environment happy-dom
 */
import JSZip from 'jszip';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { __resetAllStoresForTest, messageStore, sessionStore } from '../../src/ui/stores/stores';
import { ensureSessionBook, patchBook, setActiveSessionId } from '../../src/ui/state';
import { buildHtmlPreviewDocument, openInspectorToTab } from '../../src/ui/features/inspector';

vi.mock('../../src/ui/backend-client', () => ({
  backendApi: {
    sessionContext: vi.fn(async () => ({ used_tokens: 0, max_tokens: 0, ratio: 0 })),
  },
}));

const readTextFile = vi.fn(async () => '<!doctype html><html><head><title>Demo</title></head><body><h1>预览成功</h1></body></html>');
const readFileBase64 = vi.fn(async () => ({ base64: 'ZHVtbXk=', mimeType: 'application/octet-stream' }));
const writeTextFile = vi.fn(async () => ({ ok: true }));
const writeFileBase64 = vi.fn(async () => ({ ok: true }));

async function editableXlsxBase64(): Promise<string> {
  const zip = new JSZip();
  zip.file('xl/workbook.xml', `<?xml version="1.0"?>
    <workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
      xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
      <sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets>
    </workbook>`);
  zip.file('xl/_rels/workbook.xml.rels', `<?xml version="1.0"?>
    <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
      <Relationship Id="rId1" Target="worksheets/sheet1.xml"/>
    </Relationships>`);
  zip.file('xl/worksheets/sheet1.xml', `<?xml version="1.0"?>
    <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
      <sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>A1</t></is></c></row></sheetData>
    </worksheet>`);
  return zip.generateAsync({ type: 'base64' });
}

vi.mock('../../src/ui/office-preview', () => ({
  renderDocxPreview: vi.fn(async (_base64: string, container: HTMLElement) => {
    container.innerHTML = '<article>Word 本地预览完成</article>';
  }),
  renderPptxPreview: vi.fn(async (_base64: string, container: HTMLElement) => {
    container.innerHTML = '<article>PPT 本地预览完成</article>';
  }),
  renderXlsxPreview: vi.fn(async (_base64: string, container: HTMLElement) => {
    container.innerHTML = '<article>Excel 本地预览完成</article>';
  }),
}));

vi.mock('../../src/ui/office-edit', () => ({
  loadPptxEditBlocks: vi.fn(async () => ([{ label: '幻灯片 1 · 文本 1', text: '原始文本' }])),
  extractXlsxSheet: vi.fn(async () => ({
    name: 'Sheet1',
    rowCount: 1,
    columnCount: 1,
    cells: [{
      row: 0,
      column: 0,
      text: 'A1',
      formula: null,
      styleIndex: null,
      rowSpan: 1,
      columnSpan: 1,
    }],
    columnWidths: [96],
    rowHeights: new Map(),
    truncated: false,
  })),
  columnLabel: vi.fn((index: number) => String.fromCharCode(65 + index)),
  patchDocxBlocks: vi.fn(async () => 'ZG9jeA=='),
  patchPptxBlocks: vi.fn(async () => 'cHB0eA=='),
  patchXlsxGrid: vi.fn(async () => 'eGxzeA=='),
}));

beforeEach(() => {
  __resetAllStoresForTest();
  setActiveSessionId('sess-html');
  sessionStore.set({
    sessions: [{ id: 'sess-html', title: 'HTML', workspaceId: 'default', updatedAt: 1, preview: '', badge: '' }],
  });
  messageStore.set({
    messages: { 'sess-html': [{ id: 'u1', role: 'user', content: '做一个页面', timestamp: 1 }] },
  });
  ensureSessionBook('sess-html');
  patchBook('sess-html', {
    fileChanges: [{
      path: '/tmp/demo/index.html',
      name: 'index.html',
      added: 1,
      removed: 0,
      status: 'added',
      diff: [{ line: 1, kind: 'add', text: '<h1>预览成功</h1>' }],
    }],
  });
  window.Crew = {
    readTextFile,
    readFileBase64,
    writeTextFile,
    writeFileBase64,
    pathExists: vi.fn(async () => true),
  } as unknown as typeof window.Crew;
  document.body.innerHTML = `
    <div id="chat-inspector"><div id="chat-inspector-body"></div></div>
    <button id="task-board-toggle"></button>
    <span id="ins-files-count"></span>
  `;
  readTextFile.mockClear();
  readFileBase64.mockClear();
  writeTextFile.mockClear();
  writeFileBase64.mockClear();
});

describe('Inspector HTML 文件预览', () => {
  it('消息文件卡查看时只显示传入范围内的文件，普通打开恢复全量', () => {
    patchBook('sess-html', {
      fileChanges: [
        {
          path: '/tmp/demo/a.md',
          name: 'a.md',
          added: 1,
          removed: 0,
          status: 'added',
          diff: [{ line: 1, kind: 'add', text: 'A' }],
        },
        {
          path: '/tmp/demo/b.md',
          name: 'b.md',
          added: 1,
          removed: 0,
          status: 'added',
          diff: [{ line: 1, kind: 'add', text: 'B' }],
        },
        {
          path: '/tmp/demo/c.md',
          name: 'c.md',
          added: 1,
          removed: 0,
          status: 'added',
          diff: [{ line: 1, kind: 'add', text: 'C' }],
        },
      ],
    });

    openInspectorToTab('files', {
      expandFilePath: '/tmp/demo/deck.pptx',
      filePaths: ['/tmp/demo/a.md', '/tmp/demo/b.md', '/tmp/demo/deck.pptx'],
      fileChanges: [
        { path: '/tmp/demo/a.md', name: 'a.md', added: 1, removed: 0, status: 'added', diff: [] },
        { path: '/tmp/demo/b.md', name: 'b.md', added: 1, removed: 0, status: 'added', diff: [] },
        { path: '/tmp/demo/deck.pptx', name: 'deck.pptx', added: 0, removed: 0, status: 'added', diff: [], binary: true },
      ],
    });
    expect(document.body.textContent).toContain('deck.pptx');
    expect(document.body.textContent).toContain('a.md');
    expect(document.body.textContent).not.toContain('c.md');
    expect(document.body.textContent).toContain('本条消息改动');

    // expandFilePath 指定的产物卡在列表内联展开，不再打开独立标签页
    const deckCard = document.querySelector<HTMLElement>('.inspector-file[data-file-path="/tmp/demo/deck.pptx"]');
    expect(deckCard?.classList.contains('is-active')).toBe(true);
    expect(deckCard?.querySelector('.inspector-file__diff')).toBeTruthy();
    expect(document.querySelector('.inspector-file-tab-view')).toBeNull();

    // 点击折叠态文件卡同样在列表内联展开
    Array.from(document.querySelectorAll<HTMLButtonElement>('[data-file-toggle]'))
      .find((button) => button.textContent?.includes('a.md'))
      ?.click();
    const aCard = document.querySelector<HTMLElement>('.inspector-file[data-file-path="/tmp/demo/a.md"]');
    expect(aCard?.classList.contains('is-active')).toBe(true);
    expect(aCard?.querySelector('.inspector-file__diff')).toBeTruthy();
    expect(document.querySelector('.inspector-file-tab-view')).toBeNull();

    openInspectorToTab('files');
    expect(document.body.textContent).toContain('a.md');
    expect(document.body.textContent).toContain('b.md');
    expect(document.body.textContent).toContain('c.md');
    expect(document.body.textContent).toContain('本次会话改动');
  });

  it('历史消息只带文件路径时也能内嵌展开文件卡，并主动读取 PPT 预览内容', async () => {
    patchBook('sess-html', {
      fileChanges: [{
        path: '/tmp/demo/other.md',
        name: 'other.md',
        added: 1,
        removed: 0,
        status: 'added',
        diff: [{ line: 1, kind: 'add', text: 'other' }],
      }],
    });

    openInspectorToTab('files', {
      filePaths: ['/tmp/demo/history.pptx'],
    });
    expect(document.body.textContent).toContain('history.pptx');
    expect(document.body.textContent).toContain('本条消息改动');
    expect(document.body.textContent).not.toContain('这条消息关联的文件已不在当前改动列表中');

    const historyCard = () => document.querySelector<HTMLElement>('.inspector-file[data-file-path="/tmp/demo/history.pptx"]');
    if (!historyCard()?.classList.contains('is-active')) {
      historyCard()?.querySelector<HTMLButtonElement>('[data-file-toggle]')?.click();
    }
    expect(historyCard()?.classList.contains('is-active')).toBe(true);
    expect(historyCard()?.querySelector('.inspector-file__diff')).toBeTruthy();
    expect(document.querySelector('.inspector-file-tab-view')).toBeNull();
    await vi.waitFor(() => expect(readFileBase64).toHaveBeenCalledWith('/tmp/demo/history.pptx'));
    await vi.waitFor(() => expect(document.querySelector('.inspector-office-preview--pptx')).toBeTruthy());
    expect(document.body.textContent).not.toContain('正在加载PPT 预览');
  });

  it('HTML 文件默认展示沙箱页面，按钮可切换到代码并切回预览', async () => {
    openInspectorToTab('files', { expandFilePath: '/tmp/demo/index.html' });
    await vi.waitFor(() => expect(readTextFile).toHaveBeenCalledTimes(1));
    expect(document.getElementById('chat-inspector-body')?.innerHTML).toContain('inspector-file__preview-frame');

    const frame = document.querySelector<HTMLIFrameElement>('.inspector-file__preview-frame');
    expect(frame?.getAttribute('sandbox')).toBe('allow-scripts allow-modals');
    expect(frame?.getAttribute('srcdoc')).toContain('connect-src \'none\'');
    expect(frame?.getAttribute('srcdoc')).toContain('<base href="file:///tmp/demo/">');
    expect(document.body.textContent).toContain('查看代码');

    document.querySelector<HTMLButtonElement>('[data-file-view-toggle]')?.click();
    expect(document.querySelector('.inspector-file__preview-frame')).toBeNull();
    expect(document.querySelector('.inspector-file__diff-panel')).toBeTruthy();
    expect(document.body.textContent).toContain('查看预览');

    document.querySelector<HTMLButtonElement>('[data-file-view-toggle]')?.click();
    expect(document.querySelector('.inspector-file__preview-frame')).toBeTruthy();
    expect(readTextFile).toHaveBeenCalledTimes(1);
  });

  it('文件卡点击后在列表内嵌展开预览，不再打开独立标签页', async () => {
    openInspectorToTab('files');
    const card = () => document.querySelector<HTMLElement>('.inspector-file[data-file-path="/tmp/demo/index.html"]');
    // 统一从折叠态开始（首个文件可能被自动展开）
    if (card()?.classList.contains('is-active')) {
      card()?.querySelector<HTMLButtonElement>('[data-file-toggle]')?.click();
    }
    expect(card()?.querySelector('.inspector-file__diff')).toBeNull();
    expect(card()?.querySelector('.inspector-file__preview-frame')).toBeNull();

    card()?.querySelector<HTMLButtonElement>('[data-file-toggle]')?.click();
    expect(document.querySelector('.inspector-file-tab-view')).toBeNull();
    expect(card()?.classList.contains('is-active')).toBe(true);
    await vi.waitFor(() => expect(card()?.querySelector('.inspector-file__diff .inspector-file__preview-frame')).toBeTruthy());
  });

  it('非 HTML 文件仍直接展示代码且不出现转换按钮', () => {
    patchBook('sess-html', {
      fileChanges: [{
        path: '/tmp/demo/app.ts',
        name: 'app.ts',
        added: 1,
        removed: 0,
        status: 'added',
        diff: [{ line: 1, kind: 'add', text: 'const ok = true;' }],
      }],
    });
    openInspectorToTab('files', { expandFilePath: '/tmp/demo/app.ts' });
    expect(document.querySelector('.inspector-file__diff-panel')).toBeTruthy();
    expect(document.querySelector('[data-file-view-toggle]')).toBeNull();
  });

  it('Markdown 文件可用笔和眼切换编辑/查看，并保存回本地文件', async () => {
    readTextFile.mockResolvedValueOnce('# 原始标题');
    patchBook('sess-html', {
      fileChanges: [{
        path: '/tmp/demo/readme.md',
        name: 'readme.md',
        added: 1,
        removed: 0,
        status: 'added',
        diff: [{ line: 1, kind: 'add', text: '# 原始标题' }],
      }],
    });

    openInspectorToTab('files', { expandFilePath: '/tmp/demo/readme.md' });
    await vi.waitFor(() => expect(document.querySelector('[aria-label="切换到编辑模式"]')).toBeTruthy());
    document.querySelector<HTMLButtonElement>('[aria-label="切换到编辑模式"]')?.click();

    await vi.waitFor(() => expect(document.querySelector<HTMLTextAreaElement>('[data-file-editor="/tmp/demo/readme.md"]')).toBeTruthy());
    const editor = document.querySelector<HTMLTextAreaElement>('[data-file-editor="/tmp/demo/readme.md"]');
    if (!editor) throw new Error('没有 Markdown 编辑器');
    editor.value = '# 新标题';
    document.querySelector<HTMLButtonElement>('[data-file-save="/tmp/demo/readme.md"]')?.click();

    await vi.waitFor(() => expect(writeTextFile).toHaveBeenCalledWith('/tmp/demo/readme.md', '# 新标题'));
    await vi.waitFor(() => expect(document.querySelector('[aria-label="切换到查看模式"]')).toBeNull());
    await vi.waitFor(() => expect(document.querySelector('[aria-label="切换到编辑模式"]')).toBeTruthy());
  });

  it('Markdown 编辑后点眼睛只回到查看，不保存未保存内容', async () => {
    readTextFile.mockResolvedValueOnce('# 原始标题');
    patchBook('sess-html', {
      fileChanges: [{
        path: '/tmp/demo/draft.md',
        name: 'draft.md',
        added: 1,
        removed: 0,
        status: 'added',
        diff: [{ line: 1, kind: 'add', text: '# 原始标题' }],
      }],
    });

    openInspectorToTab('files', { expandFilePath: '/tmp/demo/draft.md' });
    await vi.waitFor(() => expect(document.querySelector('[aria-label="切换到编辑模式"]')).toBeTruthy());
    document.querySelector<HTMLButtonElement>('[aria-label="切换到编辑模式"]')?.click();
    await vi.waitFor(() => expect(document.querySelector<HTMLTextAreaElement>('[data-file-editor="/tmp/demo/draft.md"]')).toBeTruthy());
    const editor = document.querySelector<HTMLTextAreaElement>('[data-file-editor="/tmp/demo/draft.md"]');
    if (!editor) throw new Error('没有 Markdown 编辑器');
    editor.value = '# 未保存标题';
    document.querySelector<HTMLButtonElement>('[aria-label="切换到查看模式"]')?.click();

    expect(writeTextFile).not.toHaveBeenCalled();
    expect(document.querySelector('[aria-label="切换到编辑模式"]')).toBeTruthy();
    expect(document.body.textContent).not.toContain('未保存标题');
  });

  it.each([
    ['/tmp/demo/report.docx', '.inspector-office-preview--docx'],
    ['/tmp/demo/slides.pptx', '.inspector-office-preview--pptx'],
    ['/tmp/demo/table.xlsx', '.inspector-office-preview--xlsx'],
    ['/tmp/demo/report.pdf', '.inspector-file__preview-frame--pdf'],
    ['/tmp/demo/cover.png', '.inspector-file__image-preview'],
  ])('%s 作为二进制文件只展示预览，不显示代码切换', async (path, previewSelector) => {
    patchBook('sess-html', {
      fileChanges: [{
        path,
        name: path.split('/').pop() ?? path,
        added: 1,
        removed: 0,
        status: 'added',
        diff: [{ line: 1, kind: 'add', text: 'binary content' }],
      }],
    });
    openInspectorToTab('files', { expandFilePath: path });
    await vi.waitFor(() => expect(readFileBase64).toHaveBeenCalledTimes(1));
    await vi.waitFor(() => expect(document.querySelector(previewSelector)).toBeTruthy());
    expect(document.querySelector('[data-file-view-toggle]')).toBeNull();
    expect(document.body.textContent).not.toContain('查看代码');
    expect(document.querySelector('.inspector-file__diff-panel')).toBeNull();
  });

  it.each([
    '/tmp/demo/report.docx',
    '/tmp/demo/slides.pptx',
  ])('%s 编辑模式直接打开可编辑 Office 页面画布而不是表单编辑层', async (path) => {
    patchBook('sess-html', {
      fileChanges: [{
        path,
        name: path.split('/').pop() ?? path,
        added: 1,
        removed: 0,
        status: 'added',
        diff: [{ line: 1, kind: 'add', text: 'binary content' }],
      }],
    });

    openInspectorToTab('files', { expandFilePath: path });
    await vi.waitFor(() => expect(document.querySelector('[aria-label="切换到编辑模式"]')).toBeTruthy());
    document.querySelector<HTMLButtonElement>('[aria-label="切换到编辑模式"]')?.click();

    await vi.waitFor(() => expect(document.querySelector('[data-office-page-editor]')).toBeTruthy());
    expect(document.querySelector('[data-office-editor]')).toBeNull();
  });

  it('Excel 编辑器保存新增列、公式和尺寸状态', async () => {
    const path = '/tmp/demo/editable-table.xlsx';
    readFileBase64.mockResolvedValueOnce({
      base64: await editableXlsxBase64(),
      mimeType: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    });
    patchBook('sess-html', {
      fileChanges: [{
        path,
        name: 'editable-table.xlsx',
        added: 1,
        removed: 0,
        status: 'added',
        diff: [{ line: 1, kind: 'add', text: 'binary content' }],
      }],
    });
    openInspectorToTab('files', { expandFilePath: path });
    await vi.waitFor(() => expect(document.querySelector('[aria-label="切换到编辑模式"]')).toBeTruthy());
    document.querySelector<HTMLButtonElement>('[aria-label="切换到编辑模式"]')?.click();
    await vi.waitFor(() => expect(document.querySelector('[data-xlsx-editor]')).toBeTruthy());

    document.querySelector<HTMLButtonElement>('[data-xlsx-insert-column="0"]')?.click();
    document.querySelector<HTMLElement>('[data-xlsx-cell="0:1"]')?.dispatchEvent(new FocusEvent('focus'));
    const formula = document.querySelector<HTMLInputElement>('[data-xlsx-formula-input]');
    if (!formula) throw new Error('未找到公式栏');
    formula.value = '=A1';
    formula.dispatchEvent(new InputEvent('input', { bubbles: true }));
    const save = document.querySelector<HTMLButtonElement>('[data-file-save]');
    if (!save) throw new Error('未找到 Excel 保存按钮');
    save.click();
    await vi.waitFor(() => expect(writeFileBase64).toHaveBeenCalledWith(path, expect.any(String)), { timeout: 5_000 });
    const written = writeFileBase64.mock.calls.at(-1)?.[1];
    if (typeof written !== 'string') throw new Error('保存结果不是 XLSX base64');
    const writtenZip = await JSZip.loadAsync(written, { base64: true });
    const sheetXml = await writtenZip.file('xl/worksheets/sheet1.xml')?.async('text') ?? '';
    expect(sheetXml).toContain('<f>A1</f>');
    expect(sheetXml).toContain('customWidth="1"');
  });

  it('SVG 使用本地矢量图片渲染，不再创建 about:srcdoc 子框架', async () => {
    const svgPath = '/tmp/demo/diagram.svg';
    readTextFile.mockResolvedValueOnce('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 50"><rect width="100" height="50" fill="red"/></svg>');
    patchBook('sess-html', {
      fileChanges: [{
        path: svgPath,
        name: 'diagram.svg',
        added: 1,
        removed: 0,
        status: 'added',
        diff: [{ line: 1, kind: 'add', text: '<svg></svg>' }],
      }],
    });

    openInspectorToTab('files', { expandFilePath: svgPath });

    await vi.waitFor(() => {
      const image = document.querySelector<HTMLImageElement>('[data-file-svg-preview]');
      expect(image?.src).toContain('data:image/svg+xml');
    });
    expect(document.querySelector('.inspector-file__svg-preview')).toBeTruthy();
    expect(document.querySelector('.inspector-file__preview-frame')).toBeNull();
    expect(document.body.innerHTML).not.toContain('about:srcdoc');
  });

  it('重启后 Files 看板从消息 turnFileChanges 恢复终端生成的 PPT', () => {
    messageStore.set({
      messages: {
        'sess-html': [{
          id: 'result-message',
          role: 'assistant',
          content: 'PPT 已生成',
          timestamp: 2,
          turnFileChanges: [{
            path: '/tmp/demo/最终结果.pptx',
            name: '最终结果.pptx',
            added: 0,
            removed: 0,
            status: 'added',
            binary: true,
          }],
        }],
      },
    });
    patchBook('sess-html', { fileChanges: [] });

    openInspectorToTab('files');

    expect(document.body.textContent).toContain('1 个文件');
    expect(document.body.textContent).toContain('最终结果.pptx');
  });
});

describe('buildHtmlPreviewDocument', () => {
  it('为 Windows HTML 路径注入相对资源基址', () => {
    const html = buildHtmlPreviewDocument('C:\\work folder\\site\\index.html', '<html><body></body></html>');
    expect(html).toContain('<base href="file:///C:/work%20folder/site/">');
  });
});
