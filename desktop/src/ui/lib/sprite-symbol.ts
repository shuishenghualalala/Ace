/**
 * crew-ui-symbols 雪碧图引用解析。
 *
 * 构建时 esbuild 会把 symbols 以 <svg id="mw-icon-sprite"> 内联进页面
 * （见 desktop/esbuild.config.mjs）。file:// 下 Chromium 不解析外部
 * <use href="./crew-ui-symbols.svg#id">（file 源是 opaque origin，跨文件
 * 子资源引用被静默拦截），因此优先回指内联雪碧图；无内联雪碧图的宿主
 * （未走 esbuild 的静态页）保留原外部引用作为兜底。
 */
export function spriteSymbolRef(symbolRef: string): string {
  const hashIdx = symbolRef.indexOf('#');
  if (hashIdx < 0) return symbolRef;
  const id = symbolRef.slice(hashIdx + 1);
  if (!id) return symbolRef;
  const sprite = document.getElementById('mw-icon-sprite');
  if (sprite && sprite.querySelector(`#${CSS.escape(id)}`)) return `#${id}`;
  return symbolRef;
}
