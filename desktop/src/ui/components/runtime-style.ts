/**
 * Runtime geometry bridge.
 *
 * Dynamic layout values are data, not feature styling. Callers publish them
 * as allowlisted `--mw-runtime-*` variables; `runtime.css` is the single CSS
 * owner that turns those variables into declarations.
 */

const RUNTIME_PROPERTIES = new Set([
  'aspectRatio',
  'borderRadius',
  'borderBottomWidth',
  'borderLeftWidth',
  'borderRightWidth',
  'borderTopWidth',
  'bottom',
  'boxSizing',
  'color',
  'cursor',
  'fontFamily',
  'fontSize',
  'fontStyle',
  'fontVariant',
  'fontWeight',
  'height',
  'left',
  'letterSpacing',
  'lineHeight',
  'maxHeight',
  'maxWidth',
  'minHeight',
  'minWidth',
  'opacity',
  'overflow',
  'overflowWrap',
  'overflowX',
  'overflowY',
  'pointerEvents',
  'position',
  'right',
  'paddingBottom',
  'paddingLeft',
  'paddingRight',
  'paddingTop',
  'textAlign',
  'textIndent',
  'textTransform',
  'textDecoration',
  'tabSize',
  'transform',
  'top',
  'userSelect',
  'visibility',
  'whiteSpace',
  'wordSpacing',
  'width',
  'wordWrap',
  'zIndex',
  'zoom',
]);

const RUNTIME_TOKENS = new Set([
  '--mw-font-ui-size',
  '--mw-font-content-size',
  '--mw-font-editor-size',
  '--mw-font-code-size',
  '--mw-font-sans',
  '--mw-inspector-width',
  '--inspector-width',
  '--mw-task-board-width',
]);

function runtimeVariable(property: string): string {
  if (!RUNTIME_PROPERTIES.has(property)) {
    throw new Error(`Unsupported runtime style property: ${property}`);
  }
  return `--mw-runtime-${property.replace(/[A-Z]/g, (letter) => `-${letter.toLowerCase()}`)}`;
}

// ── 阻断 --mw-runtime-* 的继承泄漏 ──
// 自定义属性默认沿 DOM 树继承：在容器上 setRuntimeStyle('width', …) 会把变量静默
// 传给所有带 .mw-runtime-style 的后代——wiki 对话栏用 runtime width 持久化栏宽时，
// 栏内 composer 输入框的 width 被锁死成栏宽旧值，栏拉宽后输入框不跟随。
// 语义本应是「只作用于被 setRuntimeStyle 的那一个元素」，因此把全部变量注册为
// inherits: false（syntax '*' 的初始值即 guaranteed-invalid，var() 回退 fallback）。
if (typeof CSS !== 'undefined' && typeof CSS.registerProperty === 'function') {
  for (const property of RUNTIME_PROPERTIES) {
    try {
      CSS.registerProperty({ name: runtimeVariable(property), syntax: '*', inherits: false });
    } catch {
      // 重复注册（热更新 / 双实例模块加载）时忽略，退回继承语义，行为与修复前一致。
    }
  }
}

export function setRuntimeStyle(
  element: HTMLElement,
  property: string,
  value: string,
): void {
  element.classList.add('mw-runtime-style');
  element.style.setProperty(runtimeVariable(property), value);
  element.style.setProperty(property.replace(/[A-Z]/g, (letter) => `-${letter.toLowerCase()}`), value);
}

export function clearRuntimeStyle(element: HTMLElement, property: string): void {
  element.style.removeProperty(runtimeVariable(property));
  element.style.removeProperty(property.replace(/[A-Z]/g, (letter) => `-${letter.toLowerCase()}`));
  if (!element.getAttribute('style')) element.classList.remove('mw-runtime-style');
}

export function setRuntimeToken(element: HTMLElement, token: string, value: string): void {
  if (!RUNTIME_TOKENS.has(token)) throw new Error(`Unsupported runtime token: ${token}`);
  element.style.setProperty(token, value);
}

export function clearRuntimeToken(element: HTMLElement, token: string): void {
  if (!RUNTIME_TOKENS.has(token)) throw new Error(`Unsupported runtime token: ${token}`);
  element.style.removeProperty(token);
}
