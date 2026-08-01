export const PPTX_RUNTIME_UNSUPPORTED = 'PPTX_RUNTIME_UNSUPPORTED';

const WASM_RUNTIME_FAILURE = /Wasm init failed|WebAssembly\.instantiate|imported global does not match/i;

export function toPptxPreviewError(error: unknown): Error {
  const detail = error instanceof Error ? error.message : String(error ?? '');
  if (!WASM_RUNTIME_FAILURE.test(detail)) {
    return error instanceof Error ? error : new Error(detail || '未知错误');
  }
  const friendly = new Error(
    `当前终端未能启动本地 PPT 预览引擎（${PPTX_RUNTIME_UNSUPPORTED}）。`
    + '请完全退出并重新启动应用后重试；仍可切换到“代码”或使用 WPS/PowerPoint 打开。文件仅在本机处理，不会上传。',
  );
  friendly.cause = error;
  return friendly;
}

export interface FittedPptxSlideSvg {
  svg: string;
  width: number;
  height: number;
}

function svgAttribute(tag: string, name: string): string | null {
  const match = tag.match(new RegExp(`\\s${name}\\s*=\\s*["']([^"']+)["']`, 'i'));
  return match?.[1] ?? null;
}

function setSvgAttribute(tag: string, name: string, value: string): string {
  const pattern = new RegExp(`\\s${name}\\s*=\\s*(["'])[^"']*\\1`, 'i');
  if (pattern.test(tag)) return tag.replace(pattern, ` ${name}="${value}"`);
  return tag.replace(/\s*\/?>$/, (end) => ` ${name}="${value}"${end.trimStart()}`);
}

/** pptx-svg 输出固定 width/height 但不带 viewBox；补齐后才能随窄窗口等比缩放。 */
export function fitPptxSlideSvg(source: string): FittedPptxSlideSvg {
  const match = source.match(/<svg\b[^>]*>/i);
  if (!match) return { svg: source, width: 16, height: 9 };
  const originalTag = match[0];
  const viewBox = svgAttribute(originalTag, 'viewBox')
    ?.trim()
    .split(/[\s,]+/)
    .map(Number);
  const attrWidth = Number.parseFloat(svgAttribute(originalTag, 'width') ?? '');
  const attrHeight = Number.parseFloat(svgAttribute(originalTag, 'height') ?? '');
  const width = viewBox?.length === 4 && Number.isFinite(viewBox[2]) && viewBox[2] > 0
    ? viewBox[2]
    : Number.isFinite(attrWidth) && attrWidth > 0 ? attrWidth : 16;
  const height = viewBox?.length === 4 && Number.isFinite(viewBox[3]) && viewBox[3] > 0
    ? viewBox[3]
    : Number.isFinite(attrHeight) && attrHeight > 0 ? attrHeight : 9;

  let fittedTag = originalTag;
  if (!svgAttribute(fittedTag, 'viewBox')) {
    fittedTag = setSvgAttribute(fittedTag, 'viewBox', `0 0 ${width} ${height}`);
  }
  fittedTag = setSvgAttribute(fittedTag, 'width', '100%');
  fittedTag = setSvgAttribute(fittedTag, 'height', '100%');
  fittedTag = setSvgAttribute(fittedTag, 'preserveAspectRatio', 'xMidYMid meet');
  return {
    svg: source.replace(originalTag, fittedTag),
    width,
    height,
  };
}
