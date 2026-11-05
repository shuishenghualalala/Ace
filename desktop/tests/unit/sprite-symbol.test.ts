/**
 * spriteSymbolRef 单测：雪碧图引用在内联 sprite 可用时回指内联节点，
 * 否则保留外部引用兜底（未走 esbuild 内联的静态宿主）。
 * @vitest-environment happy-dom
 */
import { describe, expect, it } from 'vitest';
import { spriteSymbolRef } from '../../src/ui/lib/sprite-symbol';

const SPRITE_HTML = `
<svg id="mw-icon-sprite" xmlns="http://www.w3.org/2000/svg">
  <symbol id="avatar-headphones" viewBox="0 0 32 32"><path d="M16 7V4.7"></path></symbol>
</svg>`;

function mountSprite(): void {
  document.body.insertAdjacentHTML('afterbegin', SPRITE_HTML);
}

describe('spriteSymbolRef', () => {
  it('内联 sprite 存在时回指 #id', () => {
    mountSprite();
    expect(spriteSymbolRef('./crew-ui-symbols.svg#avatar-headphones')).toBe('#avatar-headphones');
    document.getElementById('mw-icon-sprite')?.remove();
  });

  it('sprite 缺少该 id 时保留原引用', () => {
    mountSprite();
    expect(spriteSymbolRef('./crew-ui-symbols.svg#missing-icon')).toBe('./crew-ui-symbols.svg#missing-icon');
    document.getElementById('mw-icon-sprite')?.remove();
  });

  it('无内联 sprite 时保留外部引用兜底', () => {
    expect(spriteSymbolRef('./crew-ui-symbols.svg#avatar-headphones')).toBe('./crew-ui-symbols.svg#avatar-headphones');
  });

  it('非雪碧图引用原样返回', () => {
    expect(spriteSymbolRef('#plain-id')).toBe('#plain-id');
    expect(spriteSymbolRef('https://example.com/x.svg#y')).toBe('https://example.com/x.svg#y');
    expect(spriteSymbolRef('no-hash')).toBe('no-hash');
  });
});
