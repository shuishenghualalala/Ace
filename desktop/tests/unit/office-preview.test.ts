/**
 * @vitest-environment happy-dom
 */
import JSZip from 'jszip';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const renderSlideSvg = vi.fn((index: number) => (
  `<svg xmlns="http://www.w3.org/2000/svg" width="960" height="540"><g style="font-family: Arial; font-size: 32px; font-weight: 700; fill: #123456"><text x="120" y="80">${index}</text></g></svg>`
));

vi.mock('docx-preview', () => ({
  renderAsync: vi.fn(async (_document: ArrayBuffer, body: HTMLElement) => {
    const wrapper = document.createElement('div');
    wrapper.className = 'docx-wrapper';
    for (let index = 1; index <= 3; index += 1) {
      const page = document.createElement('section');
      page.className = 'docx';
      const paragraph = document.createElement('p');
      paragraph.textContent = `Word 第 ${index} 页`;
      page.appendChild(paragraph);
      wrapper.appendChild(page);
    }
    body.appendChild(wrapper);
  }),
}));

vi.mock('pptx-svg', () => ({
  PptxRenderer: class {
    async init(): Promise<void> {}

    async loadPptx(): Promise<{ slideCount: number }> {
      return { slideCount: 3 };
    }

    renderSlideSvg(index: number): string {
      return renderSlideSvg(index);
    }
  },
}));

vi.mock('pptx-svg/wasm', () => ({ default: new Uint8Array() }));

import { renderDocxPreview, renderPptxPreview, renderXlsxPreview } from '../../src/ui/office-preview';

async function createXlsxBase64(): Promise<string> {
  const zip = new JSZip();
  zip.file('xl/workbook.xml', `<?xml version="1.0"?>
    <workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
      xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
      <sheets>
        <sheet name="汇总" sheetId="1" r:id="rId1"/>
        <sheet name="明细" sheetId="2" r:id="rId2"/>
      </sheets>
    </workbook>`);
  zip.file('xl/_rels/workbook.xml.rels', `<?xml version="1.0"?>
    <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
      <Relationship Id="rId1" Target="worksheets/sheet1.xml"/>
      <Relationship Id="rId2" Target="worksheets/sheet2.xml"/>
    </Relationships>`);
  zip.file('xl/worksheets/sheet1.xml', `<?xml version="1.0"?>
    <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
      <sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>汇总数据</t></is></c></row></sheetData>
    </worksheet>`);
  zip.file('xl/worksheets/sheet2.xml', `<?xml version="1.0"?>
    <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
      <sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>明细数据</t></is></c></row></sheetData>
    </worksheet>`);
  return zip.generateAsync({ type: 'base64' });
}

function button(container: HTMLElement, ariaLabel: string): HTMLButtonElement {
  const found = container.querySelector<HTMLButtonElement>(`button[aria-label="${ariaLabel}"]`);
  if (!found) throw new Error(`没有找到按钮：${ariaLabel}`);
  return found;
}

describe('Office previews', () => {
  beforeEach(() => {
    document.body.innerHTML = '<div id="preview"></div>';
    renderSlideSvg.mockClear();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('keeps Word as a continuous scrolling document with zoom controls', async () => {
    const container = document.getElementById('preview') as HTMLElement;
    await renderDocxPreview('ZHVtbXk=', container);

    expect(container.textContent).toContain('连续滚动 · 3 页');
    expect(container.querySelectorAll('section.docx')).toHaveLength(3);
    expect(container.querySelector('.inspector-office-preview__viewport--docx')).not.toBeNull();
    expect(container.querySelector('[aria-label="上一页"]')).toBeNull();

    button(container, '放大').click();
    expect(container.querySelector('.inspector-office-preview__zoom-label')?.textContent).toBe('110%');
    expect(button(container, '适应宽度').getAttribute('aria-pressed')).toBe('false');
  });

  it('makes Word paragraphs editable on the rendered page in edit mode', async () => {
    const container = document.getElementById('preview') as HTMLElement;
    await renderDocxPreview('ZHVtbXk=', container, { editable: true });

    expect(container.textContent).toContain('编辑页面 · 3 页');
    const blocks = container.querySelectorAll<HTMLElement>('[data-office-block]');
    expect(blocks).toHaveLength(3);
    expect(blocks[0].contentEditable).toBe('true');
  });

  it('renders one PPT slide at a time and pages between the first and last slide', async () => {
    let viewportWidth = 400;
    vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockImplementation(function clientWidth() {
      return this.classList.contains('inspector-office-preview__viewport--pptx') ? viewportWidth : 0;
    });
    vi.spyOn(HTMLElement.prototype, 'scrollWidth', 'get').mockImplementation(function scrollWidth() {
      return this.classList.contains('inspector-office-preview__slide') ? 960 : 0;
    });
    const container = document.getElementById('preview') as HTMLElement;
    await renderPptxPreview('ZHVtbXk=', container);
    const surface = container.querySelector<HTMLElement>('.inspector-office-preview__zoom-surface');
    const surfaceZoom = () => surface?.style.getPropertyValue('--mw-runtime-zoom') ?? '';
    await vi.waitFor(() => expect(Number.parseFloat(surfaceZoom())).toBeCloseTo(364 / 960));
    expect(container.querySelector('.inspector-office-preview__zoom-label')?.textContent).toBe('100%');

    const previous = button(container, '上一页幻灯片');
    const next = button(container, '下一页幻灯片');
    expect(container.textContent).toContain('第 1 / 3 页');
    expect(container.querySelectorAll('iframe')).toHaveLength(1);
    expect(container.querySelector<HTMLElement>('.inspector-office-preview__slide')?.style.getPropertyValue('--mw-runtime-width')).toBe('960px');
    expect(renderSlideSvg).toHaveBeenLastCalledWith(0);
    expect(previous.disabled).toBe(true);
    expect(next.disabled).toBe(false);

    next.click();
    expect(container.textContent).toContain('第 2 / 3 页');
    expect(renderSlideSvg).toHaveBeenLastCalledWith(1);
    expect(container.querySelector('iframe')?.title).toBe('幻灯片 2');

    next.click();
    expect(container.textContent).toContain('第 3 / 3 页');
    expect(renderSlideSvg).toHaveBeenLastCalledWith(2);
    expect(next.disabled).toBe(true);

    previous.click();
    expect(container.textContent).toContain('第 2 / 3 页');
    button(container, '放大').click();
    expect(container.querySelector('.inspector-office-preview__zoom-label')?.textContent).toBe('110%');
    expect(Number.parseFloat(surfaceZoom())).toBeCloseTo((364 / 960) * 1.1);
    button(container, '缩小').click();
    expect(container.querySelector('.inspector-office-preview__zoom-label')?.textContent).toBe('100%');
    button(container, '放大').click();
    expect(container.querySelector('.inspector-office-preview__zoom-label')?.textContent).toBe('110%');
    viewportWidth = 700;
    window.dispatchEvent(new CustomEvent('inspector:layout-changed'));
    await vi.waitFor(() => expect(Number.parseFloat(surfaceZoom())).toBeCloseTo((664 / 960) * 1.1));
    expect(container.querySelector('.inspector-office-preview__zoom-label')?.textContent).toBe('110%');
    const fitWidth = button(container, '适应宽度');
    fitWidth.click();
    await vi.waitFor(() => expect(fitWidth.getAttribute('aria-pressed')).toBe('true'));
    expect(container.querySelector('.inspector-office-preview__zoom-label')?.textContent).toBe('100%');
  });

  it('makes PPT text editable on the rendered slide in edit mode', async () => {
    const container = document.getElementById('preview') as HTMLElement;
    await renderPptxPreview('ZHVtbXk=', container, { editable: true });

    expect(container.textContent).toContain('第 1 / 3 页');
    expect(container.querySelector('iframe')).toBeNull();
    const stage = container.querySelector<HTMLElement>('.inspector-office-preview__slide');
    const shadow = stage?.shadowRoot;
    expect(shadow).toBeTruthy();
    const text = shadow!.querySelector<HTMLTextAreaElement>('.inspector-office-page-editor__ppt-textbox[data-office-block]');
    expect(text?.tagName).toBe('TEXTAREA');
    expect(text?.value).toBe('0');
    expect(text?.dataset.officeBlock).toBe('0');
    const svgText = shadow!.querySelector('text');
    expect(svgText?.parentElement?.getAttribute('style')).toContain('font-family: Arial');
    expect(svgText?.getAttribute('aria-hidden')).toBeNull();
    text!.dispatchEvent(new FocusEvent('focus'));
    expect(svgText?.classList.contains('is-dirty')).toBe(false);
    text!.value = '新标题';
    text!.dispatchEvent(new InputEvent('input', { bubbles: true }));
    expect(svgText?.textContent).toBe('0');
    expect(shadow!.querySelector('.inspector-office-page-editor__ppt-edit-preview')).toBeNull();
    expect(text?.value).toBe('新标题');
    expect(text?.classList.contains('is-dirty')).toBe(true);
    expect(text?.dataset.minWidthPx).not.toBeUndefined();
    expect(text?.dataset.maxWidthPx).not.toBeUndefined();
    expect(svgText?.classList.contains('is-dirty')).toBe(true);

    button(container, '下一页幻灯片').click();
    const nextText = stage!.shadowRoot!.querySelector<HTMLTextAreaElement>('.inspector-office-page-editor__ppt-textbox[data-office-block]');
    expect(nextText?.value).toBe('1');
    expect(nextText?.dataset.officeBlock).toBe('1');
  });

  it('keeps PPT rendered tspan styles as the visible source in edit mode', async () => {
    renderSlideSvg.mockImplementation((index: number) => (
      `<svg xmlns="http://www.w3.org/2000/svg" width="960" height="540">
        <text x="480" y="180" font-family="Calibri, sans-serif" font-size="18" fill="black" text-anchor="middle">
          <tspan x="480" y="180" font-family="Microsoft YaHei" font-size="40" font-weight="700" fill="rgb(30,60,80)">${index === 0 ? '产品招聘看板平台' : index}</tspan>
        </text>
      </svg>`
    ));
    // happy-dom 把 shadow 的 :host{width/height:100%} 应用到宿主 computed style，
    // 真实浏览器里 960px 由 runtime.css 的 .mw-runtime-style 规则承接，这里 mock 布局尺寸。
    vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockImplementation(function clientWidth() {
      return this.classList.contains('inspector-office-preview__slide') ? 960 : 0;
    });
    vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockImplementation(function clientHeight() {
      return this.classList.contains('inspector-office-preview__slide') ? 540 : 0;
    });
    const container = document.getElementById('preview') as HTMLElement;
    await renderPptxPreview('ZHVtbXk=', container, { editable: true });

    const shadow = container.querySelector<HTMLElement>('.inspector-office-preview__slide')?.shadowRoot;
    const sourceTspan = shadow?.querySelector('tspan');
    const overlay = shadow?.querySelector<HTMLTextAreaElement>('.inspector-office-page-editor__ppt-textbox[data-office-block]');
    const shadowStyles = shadow?.querySelector('style')?.textContent ?? '';
    expect(sourceTspan?.getAttribute('font-family')).toBe('Microsoft YaHei');
    expect(sourceTspan?.getAttribute('font-size')).toBe('40');
    expect(sourceTspan?.getAttribute('font-weight')).toBe('700');
    expect(sourceTspan?.getAttribute('fill')).toBe('rgb(30,60,80)');
    expect(shadowStyles).toContain('color:transparent');
    expect(shadowStyles).not.toContain('color:transparent!important');
    expect(shadowStyles).toContain('resize:none');
    expect(shadowStyles).toContain('ppt-textbox.is-dirty{color:var(--mw-runtime-color,currentColor)}');
    expect(shadowStyles).not.toContain('ppt-edit-preview');
    expect(overlay?.value).toContain('产品招聘看板平台');
    expect(overlay?.style.getPropertyValue('--mw-runtime-font-family')).toContain('Microsoft YaHei');
    expect(overlay?.style.getPropertyValue('--mw-runtime-font-size')).toBe('40px');
    expect(overlay?.style.getPropertyValue('--mw-runtime-font-weight')).toBe('700');
    expect(overlay?.style.getPropertyValue('--mw-runtime-color')).toBe('rgb(30,60,80)');
  });

  it('scales SVG text box metadata and pt units into the PPT editing layer', async () => {
    renderSlideSvg.mockImplementation(() => (
      `<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720">
        <text x="590" y="415" data-width="260" data-height="22" text-anchor="middle" font-family="Microsoft YaHei" font-size="12pt" fill="#334E5C">BOSS直聘 35%</text>
      </svg>`
    ));
    // happy-dom 把 shadow 的 :host{width/height:100%} 应用到宿主 computed style，
    // 真实浏览器里 960px 由 runtime.css 的 .mw-runtime-style 规则承接，这里 mock 布局尺寸。
    vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockImplementation(function clientWidth() {
      return this.classList.contains('inspector-office-preview__slide') ? 960 : 0;
    });
    vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockImplementation(function clientHeight() {
      return this.classList.contains('inspector-office-preview__slide') ? 540 : 0;
    });
    const container = document.getElementById('preview') as HTMLElement;
    await renderPptxPreview('ZHVtbXk=', container, { editable: true });

    const overlay = container.querySelector<HTMLElement>('.inspector-office-preview__slide')
      ?.shadowRoot
      ?.querySelector<HTMLTextAreaElement>('.inspector-office-page-editor__ppt-textbox[data-office-block]');
    expect(overlay?.style.getPropertyValue('--mw-runtime-font-size')).toBe('12px');
    expect(overlay?.style.getPropertyValue('--mw-runtime-width')).toBe('195px');
    expect(overlay?.style.getPropertyValue('--mw-runtime-height')).toBe('16.5px');
    expect(Number.parseFloat(overlay?.dataset.minWidthPx ?? '')).toBeGreaterThanOrEqual(195);
    expect(overlay?.style.getPropertyValue('--mw-runtime-font-family')).toContain('Microsoft YaHei');
  });

  it('shows one Excel worksheet at a time and switches sheets without stacking them', async () => {
    const container = document.getElementById('preview') as HTMLElement;
    await renderXlsxPreview(await createXlsxBase64(), container);

    const select = container.querySelector<HTMLSelectElement>('select[aria-label="选择工作表"]');
    expect(select?.options).toHaveLength(2);
    expect(container.querySelectorAll('table')).toHaveLength(1);
    expect(container.textContent).toContain('汇总数据');
    expect(container.textContent).not.toContain('明细数据');

    if (!select) throw new Error('没有找到工作表选择器');
    select.value = '1';
    select.dispatchEvent(new Event('change'));
    await vi.waitFor(() => expect(container.textContent).toContain('明细数据'));
    expect(container.querySelectorAll('table')).toHaveLength(1);
    expect(container.textContent).not.toContain('汇总数据');

    button(container, '缩小').click();
    expect(container.querySelector('.inspector-office-preview__zoom-label')?.textContent).toBe('90%');
    expect(container.querySelector<HTMLElement>('.inspector-office-preview__xlsx-content')?.style.getPropertyValue('--mw-runtime-transform')).toBe('scale(0.9)');
    expect(container.querySelector<HTMLElement>('.inspector-office-preview__xlsx-sizer')?.style.getPropertyValue('--mw-runtime-width')).not.toBe('');
    expect(button(container, '适应宽度')).not.toBeNull();
    expect(container.querySelector<HTMLElement>('.inspector-office-preview__zoom-surface')).toBeNull();
  });

  it('keeps Excel zoom relative to one stable unscaled worksheet size', async () => {
    vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockImplementation(function clientWidth() {
      return this.classList.contains('inspector-office-preview__viewport--xlsx') ? 500 : 0;
    });
    vi.spyOn(HTMLElement.prototype, 'scrollWidth', 'get').mockImplementation(function scrollWidth() {
      return this.classList.contains('inspector-xlsx-preview__table') ? 300 : 0;
    });
    vi.spyOn(HTMLElement.prototype, 'scrollHeight', 'get').mockImplementation(function scrollHeight() {
      return this.classList.contains('inspector-office-preview__xlsx-content') ? 200 : 0;
    });
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function getBoundingClientRect() {
      const content = this.closest<HTMLElement>('.inspector-office-preview__xlsx-content');
      const scale = Number.parseFloat(
        content?.style.getPropertyValue('--mw-runtime-transform').match(/scale\(([^)]+)\)/)?.[1] ?? '1',
      );
      const width = this.classList.contains('inspector-xlsx-preview__table') ? 300 * scale : 0;
      return {
        x: 0,
        y: 0,
        width,
        height: 0,
        top: 0,
        right: width,
        bottom: 0,
        left: 0,
        toJSON: () => ({}),
      };
    });

    const container = document.getElementById('preview') as HTMLElement;
    await renderXlsxPreview(await createXlsxBase64(), container);
    const sizer = container.querySelector<HTMLElement>('.inspector-office-preview__xlsx-sizer');
    const content = container.querySelector<HTMLElement>('.inspector-office-preview__xlsx-content');
    const sizerWidth = () => sizer?.style.getPropertyValue('--mw-runtime-width');
    const contentTransform = () => content?.style.getPropertyValue('--mw-runtime-transform');

    await vi.waitFor(() => expect(sizerWidth()).toBe('300px'));
    button(container, '放大').click();
    button(container, '放大').click();
    expect(container.querySelector('.inspector-office-preview__zoom-label')?.textContent).toBe('120%');
    expect(contentTransform()).toBe('scale(1.2)');
    expect(sizerWidth()).toBe('360px');

    button(container, '放大').click();
    button(container, '放大').click();
    expect(container.querySelector('.inspector-office-preview__zoom-label')?.textContent).toBe('140%');
    expect(contentTransform()).toBe('scale(1.4)');
    expect(sizerWidth()).toBe('420px');

    button(container, '适应宽度').click();
    await vi.waitFor(() => expect(contentTransform()).toBe('scale(1)'));
    expect(container.querySelector('.inspector-office-preview__zoom-label')?.textContent).toBe('100%');
    expect(sizerWidth()).toBe('300px');
    expect(button(container, '适应宽度').getAttribute('aria-pressed')).toBe('true');
  });
});
