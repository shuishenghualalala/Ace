/**
 * 量算 textarea 光标的像素坐标，以及把 textarea 的文本计算样式复制到镜像节点。
 *
 * 用途：触发式补全浮层按「光标 / 触发符」像素位置定位；chip 覆盖层与 textarea
 * 像素级对齐。二者共用经典的「镜像 div」技法——复制字体与盒模型到一个镜像节点，
 * 使其换行/字宽与 textarea 完全一致，再量算标记位。
 *
 * 注意：本模块是纯几何工具，不含任何业务/补全逻辑，可独立单测。
 */

/** 需要 1:1 复制以保证换行一致的「文本 + 盒模型」计算属性（camelCase）。 */
const COPIED_STYLE_PROPS = [
  'fontFamily',
  'fontSize',
  'fontWeight',
  'fontStyle',
  'fontVariant',
  'letterSpacing',
  'lineHeight',
  'wordSpacing',
  'textTransform',
  'textAlign',
  'textIndent',
  'tabSize',
  'boxSizing',
  'paddingTop',
  'paddingRight',
  'paddingBottom',
  'paddingLeft',
  'borderTopWidth',
  'borderRightWidth',
  'borderBottomWidth',
  'borderLeftWidth',
] as const;

/**
 * 把 src（textarea）的文本/盒模型计算样式复制到 target（镜像节点），
 * 使二者换行、字宽、行高一致。并强制 pre-wrap + 断词，匹配 textarea 的软换行语义。
 * 宽度对齐到 src.clientWidth（内容盒宽）。
 */
export function copyTextareaStyle(src: HTMLTextAreaElement, target: HTMLElement): void {
  const cs = getComputedStyle(src);
  const srcRec = cs as unknown as Record<string, string>;
  const dst = target.style as unknown as Record<string, string>;
  for (const prop of COPIED_STYLE_PROPS) dst[prop] = srcRec[prop];
  target.style.whiteSpace = 'pre-wrap';
  target.style.wordWrap = 'break-word';
  target.style.overflowWrap = 'break-word';
  target.style.width = `${src.clientWidth}px`;
}

let caretMirror: HTMLDivElement | null = null;

/** 复用同一个隐藏镜像 div（避免每次量算都建/删节点）。 */
function getCaretMirror(): HTMLDivElement {
  if (!caretMirror) {
    caretMirror = document.createElement('div');
    caretMirror.setAttribute('aria-hidden', 'true');
    caretMirror.style.position = 'absolute';
    caretMirror.style.visibility = 'hidden';
    caretMarkerStyleFallback(caretMirror);
    caretMirror.style.top = '0';
    caretMirror.style.left = '0';
    caretMirror.style.pointerEvents = 'none';
    caretMirror.style.zIndex = '-1';
    document.body.appendChild(caretMirror);
  }
  return caretMirror;
}

/** 兜底：visibility:hidden 时部分浏览器仍渲染空白，叠加 0 尺寸避免影响布局。 */
function caretMarkerStyleFallback(el: HTMLElement): void {
  el.style.height = '0';
  el.style.overflow = 'hidden';
}

export interface CaretCoords {
  /** 相对 textarea 内容盒左上角的纵坐标（已加回 scrollTop）。 */
  top: number;
  /** 相对 textarea 内容盒左上角的横坐标（已加回 scrollLeft）。 */
  left: number;
  /** 行高估算（光标高度）。 */
  height: number;
}

/**
 * 量算 textarea 中 `atIndex` 位置的像素坐标，相对 textarea 内容盒左上角。
 * 不修改 textarea 的 selectionStart/End（用独立镜像量算，无副作用）。
 * 默认 atIndex = 当前 selectionStart。
 */
export function getCaretCoords(textarea: HTMLTextAreaElement, atIndex?: number): CaretCoords | null {
  const idx = atIndex ?? textarea.selectionStart;
  if (idx == null || idx < 0 || idx > textarea.value.length) return null;
  const mirror = getCaretMirror();
  copyTextareaStyle(textarea, mirror);
  mirror.textContent = '';

  const value = textarea.value;
  const before = document.createTextNode(value.slice(0, idx));
  const marker = document.createElement('span');
  // 零宽空格：占一个光标宽度的可量算位置，且对换行影响最小。
  marker.textContent = '​';
  const after = document.createTextNode(value.slice(idx));
  mirror.append(before, marker, after);

  const markerRect = marker.getBoundingClientRect();
  const mirrorRect = mirror.getBoundingClientRect();
  const cs = getComputedStyle(textarea);
  return {
    top: markerRect.top - mirrorRect.top + textarea.scrollTop,
    left: markerRect.left - mirrorRect.left + textarea.scrollLeft,
    height: markerRect.height || parseFloat(cs.lineHeight) || 16,
  };
}
