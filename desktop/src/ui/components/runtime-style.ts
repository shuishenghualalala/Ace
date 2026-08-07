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
  '--mw-task-board-width',
]);

function runtimeVariable(property: string): string {
  if (!RUNTIME_PROPERTIES.has(property)) {
    throw new Error(`Unsupported runtime style property: ${property}`);
  }
  return `--mw-runtime-${property.replace(/[A-Z]/g, (letter) => `-${letter.toLowerCase()}`)}`;
}

export function setRuntimeStyle(
  element: HTMLElement,
  property: string,
  value: string,
): void {
  element.classList.add('mw-runtime-style');
  element.style.setProperty(runtimeVariable(property), value);
}

export function clearRuntimeStyle(element: HTMLElement, property: string): void {
  element.style.removeProperty(runtimeVariable(property));
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
