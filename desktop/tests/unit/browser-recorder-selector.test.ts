import { describe, expect, it } from 'vitest';

import { sanitizeSelectorFragment } from '../../src/main/browser-recorder';

describe('sanitizeSelectorFragment', () => {
  it('保留正常 CSS 路径', () => {
    expect(sanitizeSelectorFragment('#ticket > div:nth-of-type(2) > button:nth-of-type(1)'))
      .toBe('#ticket > div:nth-of-type(2) > button:nth-of-type(1)');
    expect(sanitizeSelectorFragment('#app')).toBe('#app');
  });

  it('完整保留 Playwright 分段与内部选择器语法', () => {
    expect(sanitizeSelectorFragment('#a >> #b')).toBe('#a >> #b');
    expect(sanitizeSelectorFragment('#a >> internal:control=enter-frame >> #b'))
      .toBe('#a >> internal:control=enter-frame >> #b');
  });

  it('保留 Playwright 支持的所有选择器引擎', () => {
    for (const selector of [
      'internal:role=button[name="删除"i]',
      'css=#a',
      'xpath=//button',
      'text=确认',
      'aria-ref=e5',
    ]) {
      expect(sanitizeSelectorFragment(selector)).toBe(selector);
    }
  });

  it('不截断超长选择器，只拒绝非字符串形状', () => {
    const selector = '#' + 'a'.repeat(100_000);
    expect(sanitizeSelectorFragment(selector)).toBe(selector);
    expect(sanitizeSelectorFragment(null)).toBe('');
    expect(sanitizeSelectorFragment(undefined)).toBe('');
    expect(sanitizeSelectorFragment(42)).toBe('');
  });

  it('保留 Unicode、换行、引号与转义字符', () => {
    for (const selector of ['#中文', '#a\n#b', '#a`b', '#a{b}', String.raw`#a\\:b`]) {
      expect(sanitizeSelectorFragment(selector)).toBe(selector);
    }
  });
});
