import DOMPurify from 'dompurify';
import { renderAsync as renderDocxAsync } from 'docx-preview';
import { PptxRenderer } from 'pptx-svg';
import pptxWasm from 'pptx-svg/wasm';

import { base64ToArrayBuffer, buildOfflinePreviewDocument } from './file-preview';
import { fitPptxSlideSvg, toPptxPreviewError } from './pptx-runtime';
import { loadXlsxPreviewWorkbook, type XlsxPreviewSheet } from './xlsx-preview';

const MIN_ZOOM = 0.5;
const MAX_ZOOM = 2;
const ZOOM_STEP = 0.1;
const PPTX_BASE_WIDTH = 960;

interface OfficeZoomController {
  fitWidth(): void;
  refreshFit(): void;
  setTarget(target: HTMLElement | null): void;
}

interface OfficePreviewOptions {
  editable?: boolean;
}

const OFFICE_LAYOUT_EVENT = 'inspector:layout-changed';

function bindOfficeLayoutRefresh(container: HTMLElement, zoom: OfficeZoomController): void {
  const refresh = (): void => zoom.refreshFit();
  window.addEventListener(OFFICE_LAYOUT_EVENT, refresh);
  window.addEventListener('resize', refresh);
  const observer = new MutationObserver(() => {
    if (document.body.contains(container)) return;
    window.removeEventListener(OFFICE_LAYOUT_EVENT, refresh);
    window.removeEventListener('resize', refresh);
    observer.disconnect();
  });
  observer.observe(document.body, { childList: true, subtree: true });
}

interface PptxTextOverlayMetrics {
  maxWidthPx: number;
  minWidthPx: number;
}

function toolbarButton(label: string, ariaLabel: string): HTMLButtonElement {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'inspector-office-preview__toolbar-button';
  button.textContent = label;
  button.setAttribute('aria-label', ariaLabel);
  return button;
}

function createOfficeToolbar(ariaLabel: string): { toolbar: HTMLDivElement; leading: HTMLDivElement } {
  const toolbar = document.createElement('div');
  toolbar.className = 'inspector-office-preview__toolbar';
  toolbar.setAttribute('aria-label', ariaLabel);
  const leading = document.createElement('div');
  leading.className = 'inspector-office-preview__toolbar-group';
  toolbar.appendChild(leading);
  return { toolbar, leading };
}

function createZoomController(
  toolbar: HTMLElement,
  viewport: HTMLElement,
  surface: HTMLElement,
): OfficeZoomController {
  const group = document.createElement('div');
  group.className = 'inspector-office-preview__toolbar-group inspector-office-preview__zoom-controls';
  const zoomOut = toolbarButton('−', '缩小');
  const label = document.createElement('span');
  label.className = 'inspector-office-preview__zoom-label';
  label.setAttribute('aria-live', 'polite');
  const zoomIn = toolbarButton('+', '放大');
  const fitWidthButton = toolbarButton('适应宽度', '适应宽度');
  fitWidthButton.classList.add('inspector-office-preview__fit-width');
  fitWidthButton.setAttribute('aria-pressed', 'true');
  group.append(zoomOut, label, zoomIn, fitWidthButton);
  toolbar.appendChild(group);

  let zoom = 1;
  let fitScale = 1;
  let fitMode = true;
  let target: HTMLElement | null = null;
  const clampZoom = (next: number): number => Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, Math.round(next * 10) / 10));
  const computeFitScale = (): number => {
    surface.style.setProperty('zoom', '1');
    const viewportWidth = Math.max(0, viewport.clientWidth - 36);
    const targetWidth = target
      ? Math.max(target.scrollWidth, target.getBoundingClientRect().width)
      : viewportWidth;
    return viewportWidth > 0 && targetWidth > 0 ? Math.min(1, viewportWidth / targetWidth) : 1;
  };
  const apply = (next: number): void => {
    zoom = clampZoom(next);
    surface.style.setProperty('zoom', String(fitScale * zoom));
    label.textContent = `${Math.round(zoom * 100)}%`;
    zoomOut.disabled = zoom <= MIN_ZOOM;
    zoomIn.disabled = zoom >= MAX_ZOOM;
    fitWidthButton.setAttribute('aria-pressed', String(fitMode));
  };
  const calculateFit = (): void => {
    fitScale = computeFitScale();
    zoom = 1;
    fitMode = true;
    apply(1);
  };
  const refreshFitScale = (): void => {
    const currentZoom = zoom;
    fitScale = computeFitScale();
    apply(currentZoom);
  };
  const scheduleFit = (): void => {
    const run = (): void => calculateFit();
    if (typeof window.requestAnimationFrame === 'function') window.requestAnimationFrame(run);
    else window.setTimeout(run, 0);
  };

  zoomOut.addEventListener('click', () => {
    fitMode = false;
    apply(zoom - ZOOM_STEP);
  });
  zoomIn.addEventListener('click', () => {
    fitMode = false;
    apply(zoom + ZOOM_STEP);
  });
  fitWidthButton.addEventListener('click', scheduleFit);
  apply(1);

  return {
    fitWidth: scheduleFit,
    refreshFit: () => {
      const run = (): void => refreshFitScale();
      if (typeof window.requestAnimationFrame === 'function') window.requestAnimationFrame(run);
      else window.setTimeout(run, 0);
    },
    setTarget: (nextTarget) => { target = nextTarget; },
  };
}

function createXlsxZoomController(
  toolbar: HTMLElement,
  viewport: HTMLElement,
  sizer: HTMLElement,
  content: HTMLElement,
): OfficeZoomController {
  const group = document.createElement('div');
  group.className = 'inspector-office-preview__toolbar-group inspector-office-preview__zoom-controls';
  const zoomOut = toolbarButton('−', '缩小');
  const label = document.createElement('span');
  label.className = 'inspector-office-preview__zoom-label';
  label.setAttribute('aria-live', 'polite');
  const zoomIn = toolbarButton('+', '放大');
  const fitWidthButton = toolbarButton('适应宽度', '适应宽度');
  fitWidthButton.classList.add('inspector-office-preview__fit-width');
  fitWidthButton.setAttribute('aria-pressed', 'true');
  group.append(zoomOut, label, zoomIn, fitWidthButton);
  toolbar.appendChild(group);

  let zoom = 1;
  let fitScale = 1;
  let fitMode = true;
  let target: HTMLElement | null = null;
  let measuredSize: { width: number; height: number } | null = null;
  const clampZoom = (next: number): number => Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, Math.round(next * 10) / 10));

  const measureNaturalSize = (): { width: number; height: number } => {
    content.style.transform = 'scale(1)';
    sizer.style.removeProperty('width');
    sizer.style.removeProperty('height');
    const width = target ? Math.max(target.scrollWidth, target.getBoundingClientRect().width) : content.scrollWidth;
    const height = content.scrollHeight;
    measuredSize = { width: Math.max(1, width), height: Math.max(1, height) };
    return measuredSize;
  };

  const apply = (next: number): void => {
    zoom = clampZoom(next);
    const scale = fitScale * zoom;
    const { width, height } = measuredSize ?? measureNaturalSize();
    content.style.transform = `scale(${scale})`;
    sizer.style.width = `${Math.ceil(width * scale)}px`;
    sizer.style.height = `${Math.ceil(height * scale)}px`;
    label.textContent = `${Math.round(zoom * 100)}%`;
    zoomOut.disabled = zoom <= MIN_ZOOM;
    zoomIn.disabled = zoom >= MAX_ZOOM;
    fitWidthButton.setAttribute('aria-pressed', String(fitMode));
  };

  const calculateFit = (): void => {
    const viewportWidth = Math.max(0, viewport.clientWidth - 36);
    const { width } = measureNaturalSize();
    fitScale = viewportWidth > 0 && width > 0 ? Math.min(1, viewportWidth / width) : 1;
    zoom = 1;
    fitMode = true;
    apply(1);
  };
  const refreshFitScale = (): void => {
    const currentZoom = zoom;
    const viewportWidth = Math.max(0, viewport.clientWidth - 36);
    const { width } = measureNaturalSize();
    fitScale = viewportWidth > 0 && width > 0 ? Math.min(1, viewportWidth / width) : 1;
    apply(currentZoom);
  };

  const scheduleFit = (): void => {
    const run = (): void => calculateFit();
    if (typeof window.requestAnimationFrame === 'function') window.requestAnimationFrame(run);
    else window.setTimeout(run, 0);
  };

  zoomOut.addEventListener('click', () => {
    fitMode = false;
    apply(zoom - ZOOM_STEP);
  });
  zoomIn.addEventListener('click', () => {
    fitMode = false;
    apply(zoom + ZOOM_STEP);
  });
  fitWidthButton.addEventListener('click', scheduleFit);
  apply(1);

  return {
    fitWidth: scheduleFit,
    refreshFit: () => {
      const run = (): void => refreshFitScale();
      if (typeof window.requestAnimationFrame === 'function') window.requestAnimationFrame(run);
      else window.setTimeout(run, 0);
    },
    setTarget: (nextTarget) => {
      target = nextTarget;
      measuredSize = null;
    },
  };
}

function markDocxPageEditable(renderHost: HTMLElement): void {
  const paragraphs = Array.from(renderHost.querySelectorAll<HTMLElement>('section.docx p'))
    .filter((paragraph) => (paragraph.textContent ?? '').trim().length > 0);
  paragraphs.forEach((paragraph, index) => {
    paragraph.contentEditable = 'true';
    paragraph.spellcheck = false;
    paragraph.dataset.officeBlock = String(index);
    paragraph.classList.add('inspector-office-page-editor__block');
    paragraph.setAttribute('role', 'textbox');
    paragraph.setAttribute('aria-label', `Word 段落 ${index + 1}`);
  });
}

function svgTextNumberAttribute(node: Element, name: string): number | null {
  const raw = node.getAttribute(name)?.split(/\s+/)[0];
  if (!raw) return null;
  if (raw.trim().endsWith('%')) return null;
  const value = Number.parseFloat(raw);
  return Number.isFinite(value) ? value : null;
}

function svgCssLengthToPx(raw: string | null): number | null {
  if (!raw) return null;
  const value = Number.parseFloat(raw);
  if (!Number.isFinite(value)) return null;
  const unit = raw.trim().replace(String(value), '').trim().toLowerCase();
  if (unit === 'pt') return value * (4 / 3);
  if (unit === 'em' || unit === 'rem') return value * 16;
  return value;
}

function svgTextStyle(node: Element, name: string): string | null {
  const direct = node.getAttribute(name);
  if (direct) return direct;
  const style = node.getAttribute('style') ?? '';
  const found = style.split(';')
    .map((part) => part.split(':').map((item) => item.trim()))
    .find(([key]) => key === name);
  return found?.[1] ?? null;
}

function inheritedSvgTextStyle(node: Element, name: string): string | null {
  let current: Element | null = node;
  while (current) {
    const value = svgTextStyle(current, name);
    if (value) return value;
    current = current.parentElement;
  }
  return null;
}

function styledSvgTextSource(node: SVGTextElement): SVGTextElement {
  const candidates = [...Array.from(node.querySelectorAll<SVGTextElement>('tspan')), node];
  const scored = candidates
    .map((candidate, index) => {
      const directScore = ['font-size', 'font-family', 'font-weight', 'fill']
        .filter((name) => svgTextStyle(candidate, name)).length;
      const inheritedScore = ['font-size', 'font-family', 'font-weight', 'fill']
        .filter((name) => inheritedSvgTextStyle(candidate, name)).length;
      const textLength = (candidate.textContent ?? '').trim().length;
      return { candidate, directScore, inheritedScore, textLength, index };
    })
    .sort((a, b) => (
      b.directScore - a.directScore
      || b.inheritedScore - a.inheritedScore
      || b.textLength - a.textLength
      || a.index - b.index
    ));
  return scored[0]?.candidate ?? node;
}

function numericSvgTextStyle(node: Element, name: string): number | null {
  return svgCssLengthToPx(inheritedSvgTextStyle(node, name));
}

function makePptxTextOverlay(
  node: SVGTextElement,
  blockIndex: number,
  localIndex: number,
  slideWidth: number,
  slideHeight: number,
): HTMLTextAreaElement {
  const text = node.textContent ?? '';
  const styleSource = styledSvgTextSource(node);
  const box = svgTextBox(node);
  const fontSize = box.fontSize;
  const left = svgTextBoxLeft(box);
  const top = box.height ? box.y - box.height : box.y - fontSize;
  const fill = inheritedSvgTextStyle(styleSource, 'fill') ?? 'inherit';
  const fontFamily = inheritedSvgTextStyle(styleSource, 'font-family') ?? 'inherit';
  const fontWeight = inheritedSvgTextStyle(styleSource, 'font-weight') ?? 'inherit';
  const overlay = document.createElement('textarea');
  overlay.spellcheck = false;
  overlay.dataset.officeBlock = String(blockIndex);
  overlay.className = 'inspector-office-page-editor__ppt-textbox';
  overlay.setAttribute('role', 'textbox');
  overlay.setAttribute('aria-label', `PPT 文本 ${blockIndex + 1}`);
  overlay.value = text;
  overlay.textContent = text;
  overlay.dataset.originalText = text;
  overlay.style.left = `${Math.max(0, (left / slideWidth) * 100)}%`;
  overlay.style.top = `${Math.max(0, (top / slideHeight) * 100)}%`;
  overlay.style.minWidth = `${Math.min(80, Math.max(12, ((text.length * fontSize * 0.6) / slideWidth) * 100))}%`;
  overlay.dataset.minWidthPx = String(Math.max(box.width ?? 0, 80, text.length * fontSize * 0.6));
  overlay.dataset.maxWidthPx = String(Math.max(160, slideWidth - left - 12));
  if (box.width) overlay.style.width = `${box.width}px`;
  if (box.height) {
    overlay.style.height = `${box.height}px`;
    overlay.style.lineHeight = `${box.height}px`;
  }
  overlay.style.fontSize = `${Math.max(10, fontSize)}px`;
  overlay.style.setProperty('--ppt-text-color', fill);
  overlay.style.fontFamily = fontFamily;
  overlay.style.fontWeight = fontWeight;
  return overlay;
}

function svgTextBox(
  node: SVGTextElement,
): { x: number; y: number; width: number | null; height: number | null; anchor: string | null; fontSize: number } {
  const styleSource = styledSvgTextSource(node);
  const fontSize = numericSvgTextStyle(styleSource, 'font-size') ?? numericSvgTextStyle(node, 'font-size') ?? 18;
  const x = svgTextNumberAttribute(node, 'x') ?? svgTextNumberAttribute(node.querySelector('tspan') ?? node, 'x') ?? 0;
  const y = svgTextNumberAttribute(node, 'y') ?? svgTextNumberAttribute(node.querySelector('tspan') ?? node, 'y') ?? 0;
  const width = svgTextNumberAttribute(node, 'data-width') ?? svgTextNumberAttribute(styleSource, 'data-width');
  const height = svgTextNumberAttribute(node, 'data-height') ?? svgTextNumberAttribute(styleSource, 'data-height');
  const anchor = inheritedSvgTextStyle(styleSource, 'text-anchor') ?? inheritedSvgTextStyle(node, 'text-anchor');
  return { x, y, width, height, anchor, fontSize };
}

function svgTextBoxLeft(box: { x: number; width: number | null; anchor: string | null }): number {
  if (box.width && box.anchor === 'middle') return box.x - (box.width / 2);
  if (box.width && box.anchor === 'end') return box.x - box.width;
  return box.x;
}

function ownerSvgElement(node: SVGElement): SVGSVGElement | null {
  return node.ownerSVGElement ?? node.closest('svg') as SVGSVGElement | null;
}

function svgViewportSize(
  svgRoot: SVGSVGElement | null,
  fallbackWidth: number,
  fallbackHeight: number,
): { width: number; height: number } {
  const viewBox = (svgRoot?.getAttribute('viewBox') ?? svgRoot?.getAttribute('viewbox'))
    ?.trim()
    .split(/[\s,]+/)
    .map(Number);
  const viewBoxWidth = viewBox?.length === 4 && Number.isFinite(viewBox[2]) && viewBox[2] > 0 ? viewBox[2] : null;
  const viewBoxHeight = viewBox?.length === 4 && Number.isFinite(viewBox[3]) && viewBox[3] > 0 ? viewBox[3] : null;
  const width = viewBoxWidth
    ?? (svgRoot ? svgTextNumberAttribute(svgRoot, 'width') : null)
    ?? fallbackWidth;
  const height = viewBoxHeight
    ?? (svgRoot ? svgTextNumberAttribute(svgRoot, 'height') : null)
    ?? fallbackHeight;
  return {
    width: Math.max(1, width),
    height: Math.max(1, height),
  };
}

function cssPxValue(raw: string): number | null {
  const value = Number.parseFloat(raw);
  return Number.isFinite(value) ? value : null;
}

function pptxStageLayoutBox(
  stage: HTMLElement,
  svgRoot: SVGSVGElement | null,
  stageRect: DOMRect,
  slideWidth: number,
  slideHeight: number,
): { width: number; height: number; svgWidth: number; svgHeight: number } {
  const styleWidth = cssPxValue(stage.style.width);
  const viewport = svgViewportSize(svgRoot, slideWidth, slideHeight);
  const width = stage.clientWidth || styleWidth || stageRect.width || PPTX_BASE_WIDTH;
  const styleHeight = cssPxValue(stage.style.height);
  const height = stage.clientHeight
    || styleHeight
    || (width > 0 ? width * (viewport.height / viewport.width) : stageRect.height);
  return {
    width: Math.max(1, width),
    height: Math.max(1, height),
    svgWidth: viewport.width,
    svgHeight: viewport.height,
  };
}

function calibratedPptxOverlayFontSize(
  node: SVGTextElement,
  declaredFontSize: number,
  heightPx: number,
  scaleY: number,
): number {
  const scaledDeclared = Math.max(8, declaredFontSize * scaleY);
  if (!Number.isFinite(heightPx) || heightPx <= 0) return scaledDeclared;
  const textLines = Math.max(1, new Set(
    Array.from(node.querySelectorAll<SVGTSpanElement>('tspan'))
      .map((tspan) => tspan.getAttribute('y') ?? '')
      .filter(Boolean),
  ).size);
  const visualSize = (heightPx / textLines) * 0.86;
  return Math.max(8, Math.min(scaledDeclared, visualSize > 0 ? visualSize : scaledDeclared));
}

function pptxOverlayText(overlay: HTMLElement): string {
  return overlay instanceof HTMLTextAreaElement ? overlay.value : (overlay.textContent ?? '');
}

function resizePptxTextOverlayToContent(overlay: HTMLTextAreaElement, metrics: PptxTextOverlayMetrics): void {
  const mirror = document.createElement('span');
  mirror.style.position = 'absolute';
  mirror.style.visibility = 'hidden';
  mirror.style.whiteSpace = 'pre';
  mirror.style.fontFamily = overlay.style.fontFamily;
  mirror.style.fontSize = overlay.style.fontSize;
  mirror.style.fontWeight = overlay.style.fontWeight;
  mirror.style.fontStyle = overlay.style.fontStyle;
  mirror.style.letterSpacing = overlay.style.letterSpacing;
  mirror.textContent = overlay.value || ' ';
  overlay.ownerDocument.body.appendChild(mirror);
  const desiredWidth = Math.ceil(mirror.getBoundingClientRect().width + 18);
  mirror.remove();
  const nextWidth = Math.min(metrics.maxWidthPx, Math.max(metrics.minWidthPx, desiredWidth));
  overlay.style.width = `${nextWidth}px`;
  overlay.scrollLeft = overlay.scrollWidth;
}

function copyPptxTextVisualStyle(node: SVGTextElement, overlay: HTMLElement): void {
  const styleSource = styledSvgTextSource(node);
  const computed = window.getComputedStyle(styleSource);
  const attr = (name: string): string | null => inheritedSvgTextStyle(styleSource, name) ?? inheritedSvgTextStyle(node, name);
  const fill = attr('fill') ?? computed.fill ?? computed.color;
  const fontSize = attr('font-size') ?? computed.fontSize;
  const fontFamily = attr('font-family') ?? computed.fontFamily;
  const fontWeight = attr('font-weight') ?? computed.fontWeight;
  const fontStyle = attr('font-style') ?? computed.fontStyle;
  const letterSpacing = attr('letter-spacing') ?? computed.letterSpacing;
  const textDecoration = attr('text-decoration') ?? computed.textDecoration;
  const textAnchor = attr('text-anchor') ?? computed.textAnchor;
  if (fill && fill !== 'none') overlay.style.setProperty('--ppt-text-color', fill);
  if (fontSize) overlay.style.fontSize = fontSize;
  if (fontFamily) overlay.style.fontFamily = fontFamily;
  if (fontWeight) overlay.style.fontWeight = fontWeight;
  if (fontStyle) overlay.style.fontStyle = fontStyle;
  if (letterSpacing) overlay.style.letterSpacing = letterSpacing;
  if (textDecoration) overlay.style.textDecoration = textDecoration;
  if (textAnchor === 'middle') overlay.style.textAlign = 'center';
  else if (textAnchor === 'end') overlay.style.textAlign = 'right';
  else overlay.style.textAlign = 'left';
}

function alignPptxTextOverlays(
  stage: HTMLElement,
  textNodes: SVGTextElement[],
  overlays: HTMLElement[],
  slideWidth: number,
  slideHeight: number,
): void {
  const stageRect = stage.getBoundingClientRect();
  textNodes.forEach((node, index) => {
    const overlay = overlays[index];
    if (!overlay) return;
    const rect = node.getBoundingClientRect();
    const declaredBox = svgTextBox(node);
    const svgRoot = ownerSvgElement(node);
    const stageBox = pptxStageLayoutBox(stage, svgRoot, stageRect, slideWidth, slideHeight);
    const scaleX = stageBox.width / stageBox.svgWidth;
    const scaleY = stageBox.height / stageBox.svgHeight;
    const hasMeasuredRect = stageRect.width > 0 && stageRect.height > 0 && rect.width > 0 && rect.height > 0;
    const hasHeightHint = hasMeasuredRect || Boolean(declaredBox.height);
    const fallbackFontPx = Math.max(8, declaredBox.fontSize * scaleY);
    let leftPx = hasMeasuredRect
      ? Math.max(0, ((rect.left - stageRect.left) / stageRect.width) * stageBox.width)
      : Math.max(0, svgTextBoxLeft(declaredBox) * scaleX);
    const topPx = hasMeasuredRect
      ? Math.max(0, ((rect.top - stageRect.top) / stageRect.height) * stageBox.height)
      : Math.max(0, (declaredBox.y * scaleY) - (fallbackFontPx * 0.92));
    let widthPx = hasMeasuredRect ? Math.max(20, (rect.width / stageRect.width) * stageBox.width) : Math.max(80, overlay.scrollWidth);
    let heightPx = hasMeasuredRect ? Math.max(12, (rect.height / stageRect.height) * stageBox.height) : Math.max(12, fallbackFontPx * 1.25);
    if (declaredBox.width) {
      leftPx = svgTextBoxLeft(declaredBox) * scaleX;
      widthPx = declaredBox.width * scaleX;
    }
    if (declaredBox.height && !hasMeasuredRect) {
      heightPx = Math.max(heightPx, declaredBox.height * scaleY);
    }
    overlay.style.left = `${Math.max(0, (leftPx / stageBox.width) * 100)}%`;
    overlay.style.top = `${Math.max(0, (topPx / stageBox.height) * 100)}%`;
    const maxWidthPx = Math.max(widthPx, stageBox.width - leftPx - 12);
    overlay.style.width = `${widthPx}px`;
    overlay.dataset.minWidthPx = String(widthPx);
    overlay.dataset.maxWidthPx = String(maxWidthPx);
    const fontPx = hasHeightHint
      ? calibratedPptxOverlayFontSize(node, declaredBox.fontSize, heightPx, scaleY)
      : fallbackFontPx;
    heightPx = Math.max(heightPx, fontPx * 1.15);
    overlay.style.height = `${heightPx}px`;
    overlay.style.minHeight = `${heightPx}px`;
    copyPptxTextVisualStyle(node, overlay);
    overlay.style.fontSize = `${fontPx}px`;
    overlay.style.lineHeight = `${Math.max(heightPx, fontPx * 1.15)}px`;
  });
}

function countPptxSvgTextBlocks(source: string): number {
  const svg = new DOMParser().parseFromString(source, 'image/svg+xml');
  if (svg.querySelector('parsererror')) return 0;
  return Array.from(svg.getElementsByTagName('*'))
    .filter((node) => (node.localName === 'text' || node.tagName.toLowerCase() === 'text')
      && (node.textContent ?? '').trim().length > 0)
    .length;
}

function editablePptxShadowStyles(): string {
  return `<style>
    :host{display:block;width:100%;height:100%}
    svg{display:block;width:100%!important;height:100%!important;max-width:100%;max-height:100%}
    .inspector-office-page-editor__svg-source-text{pointer-events:none}
    .inspector-office-page-editor__svg-source-text.is-editing,
    .inspector-office-page-editor__svg-source-text.is-dirty{opacity:0}
    .inspector-office-page-editor__ppt-layer{position:absolute;inset:0;z-index:2;overflow:hidden;pointer-events:none}
    .inspector-office-page-editor__ppt-textbox{position:absolute;box-sizing:border-box;min-height:20px;padding:0;border:1px solid transparent;border-radius:3px;background:transparent;color:transparent!important;caret-color:#2563eb;cursor:text;line-height:normal;outline:none;overflow:hidden;pointer-events:auto;resize:none;white-space:pre}
    .inspector-office-page-editor__ppt-textbox.is-editing,
    .inspector-office-page-editor__ppt-textbox.is-dirty{color:var(--ppt-text-color,currentColor)!important}
    .inspector-office-page-editor__ppt-textbox:hover{border-color:rgba(37,99,235,.45)}
    .inspector-office-page-editor__ppt-textbox:focus{border-color:rgba(37,99,235,.85);box-shadow:0 0 0 2px rgba(37,99,235,.24)}
  </style>`;
}

function markPptxSlideEditable(
  stage: HTMLElement,
  root: ParentNode,
  slideWidth: number,
  slideHeight: number,
  blockOffset: number,
): void {
  const textNodes = Array.from(root.querySelectorAll<SVGTextElement>('*'))
    .filter((node) => (node.localName === 'text' || node.tagName.toLowerCase() === 'text')
      && (node.textContent ?? '').trim().length > 0);
  const layer = document.createElement('div');
  layer.className = 'inspector-office-page-editor__ppt-layer';
  const overlays = textNodes.map((node, index) => {
    node.classList.add('inspector-office-page-editor__svg-source-text');
    const overlay = makePptxTextOverlay(node, blockOffset + index, index, slideWidth, slideHeight);
    overlay.addEventListener('focus', () => {
      overlay.classList.add('is-editing');
      node.classList.add('is-editing');
    });
    overlay.addEventListener('blur', () => {
      overlay.classList.remove('is-editing');
      node.classList.remove('is-editing');
      const dirty = pptxOverlayText(overlay) !== (overlay.dataset.originalText ?? '');
      if (!dirty) {
        overlay.classList.remove('is-dirty');
        node.classList.remove('is-dirty');
      }
    });
    overlay.addEventListener('input', () => {
      const dirty = pptxOverlayText(overlay) !== (overlay.dataset.originalText ?? '');
      overlay.classList.toggle('is-dirty', dirty);
      node.classList.toggle('is-dirty', dirty);
      resizePptxTextOverlayToContent(overlay, {
        minWidthPx: Number.parseFloat(overlay.dataset.minWidthPx ?? '') || overlay.getBoundingClientRect().width,
        maxWidthPx: Number.parseFloat(overlay.dataset.maxWidthPx ?? '') || overlay.getBoundingClientRect().width,
      });
    });
    layer.appendChild(overlay);
    return overlay;
  });
  root.appendChild(layer);
  alignPptxTextOverlays(stage, textNodes, overlays, slideWidth, slideHeight);
  const realign = (): void => alignPptxTextOverlays(stage, textNodes, overlays, slideWidth, slideHeight);
  if (typeof window.requestAnimationFrame === 'function') window.requestAnimationFrame(realign);
  else window.setTimeout(realign, 0);
}

export async function renderDocxPreview(
  base64: string,
  container: HTMLElement,
  options: OfficePreviewOptions = {},
): Promise<void> {
  const renderHost = document.createElement('div');
  renderHost.className = 'inspector-office-preview__docx-pages';
  await renderDocxAsync(base64ToArrayBuffer(base64), renderHost, renderHost, {
    breakPages: true,
    renderHeaders: true,
    renderFooters: true,
    renderFootnotes: true,
    renderEndnotes: true,
    renderComments: false,
    renderAltChunks: false,
    useBase64URL: true,
    ignoreFonts: false,
    debug: false,
  });
  const pages = Array.from(renderHost.querySelectorAll<HTMLElement>('section.docx'));
  if (pages.length === 0) throw new Error('Word 文档中没有可预览的页面');
  if (options.editable) markDocxPageEditable(renderHost);

  const reader = document.createElement('div');
  reader.className = `inspector-office-preview__reader inspector-office-preview__reader--docx${options.editable ? ' inspector-office-page-editor inspector-office-page-editor--docx' : ''}`;
  const { toolbar, leading } = createOfficeToolbar(options.editable ? 'Word 编辑工具' : 'Word 阅读工具');
  const modeLabel = document.createElement('span');
  modeLabel.className = 'inspector-office-preview__document-label';
  modeLabel.textContent = options.editable ? `编辑页面 · ${pages.length} 页` : `连续滚动 · ${pages.length} 页`;
  leading.appendChild(modeLabel);
  const viewport = document.createElement('div');
  viewport.className = 'inspector-office-preview__viewport inspector-office-preview__viewport--docx';
  const surface = document.createElement('div');
  surface.className = 'inspector-office-preview__zoom-surface';
  surface.appendChild(renderHost);
  viewport.appendChild(surface);
  reader.append(toolbar, viewport);
  container.replaceChildren(reader);

  const zoom = createZoomController(toolbar, viewport, surface);
  zoom.setTarget(pages.reduce((widest, page) => (
    page.scrollWidth > widest.scrollWidth ? page : widest
  ), pages[0]));
  bindOfficeLayoutRefresh(container, zoom);
  zoom.fitWidth();
}

export async function renderPptxPreview(
  base64: string,
  container: HTMLElement,
  options: OfficePreviewOptions = {},
): Promise<void> {
  const renderer = new PptxRenderer({ logLevel: 'error' });
  let slideCount: number;
  try {
    await renderer.init(pptxWasm);
    ({ slideCount } = await renderer.loadPptx(base64ToArrayBuffer(base64)));
  } catch (error) {
    throw toPptxPreviewError(error);
  }
  if (slideCount < 1) {
    const empty = document.createElement('div');
    empty.className = 'inspector-file__preview-state';
    empty.textContent = 'PPT 中没有可预览的幻灯片';
    container.replaceChildren(empty);
    return;
  }

  const reader = document.createElement('div');
  reader.className = `inspector-office-preview__reader inspector-office-preview__reader--pptx${options.editable ? ' inspector-office-page-editor inspector-office-page-editor--pptx' : ''}`;
  const { toolbar, leading } = createOfficeToolbar(options.editable ? 'PPT 编辑工具' : 'PPT 阅读工具');
  const previous = toolbarButton('上一页', '上一页幻灯片');
  const label = document.createElement('span');
  label.className = 'inspector-office-preview__page-label';
  label.setAttribute('aria-live', 'polite');
  const next = toolbarButton('下一页', '下一页幻灯片');
  leading.append(previous, label, next);

  const viewport = document.createElement('div');
  viewport.className = 'inspector-office-preview__viewport inspector-office-preview__viewport--pptx';
  const surface = document.createElement('div');
  surface.className = 'inspector-office-preview__zoom-surface';
  const stage = document.createElement('section');
  stage.className = 'inspector-office-preview__slide';
  const frame = document.createElement('iframe');
  frame.className = 'inspector-office-preview__slide-frame';
  frame.setAttribute('sandbox', '');
  if (!options.editable) stage.appendChild(frame);
  surface.appendChild(stage);
  viewport.appendChild(surface);
  reader.append(toolbar, viewport);
  container.replaceChildren(reader);
  const zoom = createZoomController(toolbar, viewport, surface);
  zoom.setTarget(stage);
  bindOfficeLayoutRefresh(container, zoom);
  const editableSlideSvgs = options.editable
    ? Array.from({ length: slideCount }, (_, index) => renderer.renderSlideSvg(index))
    : [];
  const editableSlideBlockOffsets = editableSlideSvgs.reduce<number[]>((offsets, svg, index) => {
    offsets[index] = index === 0 ? 0 : offsets[index - 1] + countPptxSvgTextBlocks(editableSlideSvgs[index - 1]);
    return offsets;
  }, []);

  let currentIndex = 0;
  const showSlide = (index: number): void => {
    currentIndex = Math.min(Math.max(index, 0), slideCount - 1);
    label.textContent = `第 ${currentIndex + 1} / ${slideCount} 页`;
    frame.title = `幻灯片 ${currentIndex + 1}`;
    previous.disabled = currentIndex === 0;
    next.disabled = currentIndex === slideCount - 1;
    const rawSvg = editableSlideSvgs[currentIndex] ?? renderer.renderSlideSvg(currentIndex);
    const fittedRaw = fitPptxSlideSvg(rawSvg);
    const sanitizedSvg = DOMPurify.sanitize(fittedRaw.svg, {
      USE_PROFILES: { svg: true, svgFilters: true },
      FORBID_TAGS: ['script', 'foreignObject'],
      FORBID_ATTR: ['onload', 'onclick', 'onerror'],
    });
    const fitted = { ...fittedRaw, svg: sanitizedSvg };
    stage.style.width = `${PPTX_BASE_WIDTH}px`;
    if (options.editable) {
      stage.style.aspectRatio = `${fitted.width} / ${fitted.height}`;
      const shadow = stage.shadowRoot ?? stage.attachShadow({ mode: 'open' });
      const style = document.createElement('style');
      style.textContent = editablePptxShadowStyles();
      const slide = document.createElement('template');
      // fitted.svg was sanitized with the SVG-only DOMPurify profile above.
      slide.innerHTML = fitted.svg;
      shadow.replaceChildren(style, slide.content.cloneNode(true));
      markPptxSlideEditable(stage, shadow, fitted.width, fitted.height, editableSlideBlockOffsets[currentIndex] ?? 0);
    } else {
      frame.style.aspectRatio = `${fitted.width} / ${fitted.height}`;
      frame.srcdoc = buildOfflinePreviewDocument(
        '',
        `<style>html,body{width:100%;height:100%;margin:0;background:#fff;overflow:hidden}svg{display:block;width:100%!important;height:100%!important;max-width:100%;max-height:100%}</style>${fitted.svg}`,
      );
    }
    zoom.refreshFit();
  };

  previous.addEventListener('click', () => showSlide(currentIndex - 1));
  next.addEventListener('click', () => showSlide(currentIndex + 1));
  showSlide(0);
}

function columnLabel(index: number): string {
  let value = index + 1;
  let label = '';
  while (value > 0) {
    value -= 1;
    label = String.fromCharCode(65 + (value % 26)) + label;
    value = Math.floor(value / 26);
  }
  return label;
}

function renderXlsxTable(sheet: XlsxPreviewSheet): HTMLElement {
  const wrapper = document.createElement('div');
  wrapper.className = 'inspector-xlsx-preview__sheet';
  const table = document.createElement('table');
  table.className = 'inspector-xlsx-preview__table';
  table.setAttribute('aria-label', `工作表 ${sheet.name}`);
  const colgroup = document.createElement('colgroup');
  const rowNumberColumn = document.createElement('col');
  rowNumberColumn.style.width = '48px';
  colgroup.appendChild(rowNumberColumn);
  for (const width of sheet.columnWidths) {
    const column = document.createElement('col');
    column.style.width = `${width}px`;
    colgroup.appendChild(column);
  }
  table.appendChild(colgroup);
  const head = document.createElement('thead');
  const headRow = document.createElement('tr');
  const corner = document.createElement('th');
  corner.className = 'inspector-xlsx-preview__corner';
  corner.setAttribute('aria-hidden', 'true');
  headRow.appendChild(corner);
  for (let column = 0; column < sheet.columnCount; column += 1) {
    const heading = document.createElement('th');
    heading.scope = 'col';
    heading.textContent = columnLabel(column);
    headRow.appendChild(heading);
  }
  head.appendChild(headRow);
  table.appendChild(head);

  const byRow = new Map<number, XlsxPreviewSheet['cells']>();
  for (const cell of sheet.cells) {
    const list = byRow.get(cell.row) ?? [];
    list.push(cell);
    byRow.set(cell.row, list);
  }
  const body = document.createElement('tbody');
  for (let row = 0; row < sheet.rowCount; row += 1) {
    const tableRow = document.createElement('tr');
    const height = sheet.rowHeights.get(row);
    if (height) tableRow.style.height = `${height}px`;
    const rowHeading = document.createElement('th');
    rowHeading.scope = 'row';
    rowHeading.textContent = String(row + 1);
    tableRow.appendChild(rowHeading);
    for (const cell of byRow.get(row) ?? []) {
      const tableCell = document.createElement('td');
      tableCell.textContent = cell.text;
      if (cell.rowSpan > 1) tableCell.rowSpan = cell.rowSpan;
      if (cell.columnSpan > 1) tableCell.colSpan = cell.columnSpan;
      tableCell.title = cell.text;
      tableRow.appendChild(tableCell);
    }
    body.appendChild(tableRow);
  }
  table.appendChild(body);
  wrapper.appendChild(table);
  if (sheet.truncated) {
    const notice = document.createElement('div');
    notice.className = 'inspector-xlsx-preview__notice';
    notice.textContent = '工作表内容较大，预览仅显示前 500 行、100 列；原文件未被修改。';
    wrapper.appendChild(notice);
  }
  return wrapper;
}

export async function renderXlsxPreview(base64: string, container: HTMLElement): Promise<void> {
  const workbook = await loadXlsxPreviewWorkbook(base64);
  const reader = document.createElement('div');
  reader.className = 'inspector-office-preview__reader inspector-office-preview__reader--xlsx';
  const { toolbar, leading } = createOfficeToolbar('Excel 阅读工具');
  const sheetLabel = document.createElement('label');
  sheetLabel.className = 'inspector-xlsx-preview__sheet-picker';
  sheetLabel.append('工作表');
  const select = document.createElement('select');
  select.setAttribute('aria-label', '选择工作表');
  workbook.sheetNames.forEach((name, index) => {
    const option = document.createElement('option');
    option.value = String(index);
    option.textContent = name;
    select.appendChild(option);
  });
  sheetLabel.appendChild(select);
  leading.appendChild(sheetLabel);
  const viewport = document.createElement('div');
  viewport.className = 'inspector-office-preview__viewport inspector-office-preview__viewport--xlsx';
  const surface = document.createElement('div');
  surface.className = 'inspector-office-preview__xlsx-sizer';
  const content = document.createElement('div');
  content.className = 'inspector-office-preview__xlsx-content';
  surface.appendChild(content);
  viewport.appendChild(surface);
  reader.append(toolbar, viewport);
  container.replaceChildren(reader);
  const zoom = createXlsxZoomController(toolbar, viewport, surface, content);
  bindOfficeLayoutRefresh(container, zoom);

  let requestId = 0;
  const showSheet = async (index: number): Promise<void> => {
    const currentRequest = ++requestId;
    content.innerHTML = '<div class="inspector-file__preview-state">正在读取工作表…</div>';
    const sheet = await workbook.loadSheet(index);
    if (currentRequest !== requestId || !content.isConnected) return;
    const rendered = renderXlsxTable(sheet);
    content.replaceChildren(rendered);
    zoom.setTarget(rendered.querySelector<HTMLElement>('table'));
    zoom.fitWidth();
  };
  select.addEventListener('change', () => { void showSheet(Number.parseInt(select.value, 10)); });
  await showSheet(0);
}
