/**
 * @vitest-environment happy-dom
 *
 * tooltip.ts 自绘气泡：macOS 上替代已坏的原生 title（electron/electron#49843）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { bindTooltips } from '../../src/ui/components/tooltip';

const setUserAgent = (ua: string): void => {
  Object.defineProperty(window.navigator, 'userAgent', { value: ua, configurable: true });
};

const hover = (el: Element): void => {
  el.dispatchEvent(new MouseEvent('mouseover', { bubbles: true }));
};

describe('bindTooltips', () => {
  let dispose: () => void;

  beforeEach(() => {
    vi.useFakeTimers();
    document.body.innerHTML = '';
  });

  afterEach(() => {
    dispose?.();
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it('macOS：悬停 450ms 后显示气泡，title 转移到 data-mw-tip', () => {
    setUserAgent('Mozilla/5.0 (Macintosh; Intel Mac OS X) Electron/43.1.0');
    document.body.innerHTML = '<button id="btn" title="新建知识库">+</button>';
    dispose = bindTooltips();
    const btn = document.querySelector('#btn')!;

    hover(btn);
    // 未到延迟：不出现
    vi.advanceTimersByTime(200);
    expect(document.querySelector('.mw-tooltip')).toBeNull();
    vi.advanceTimersByTime(300);
    const tip = document.querySelector('.mw-tooltip');
    expect(tip).not.toBeNull();
    expect(tip!.textContent).toBe('新建知识库');
    expect((tip as HTMLElement).hidden).toBe(false);
    // title 被挪走，避免原生气泡偶发叠加
    expect(btn.getAttribute('title')).toBeNull();
    expect(btn.getAttribute('data-mw-tip')).toBe('新建知识库');
  });

  it('macOS：悬停到无提示元素或移出窗口后收起', () => {
    setUserAgent('Mozilla/5.0 (Macintosh; Intel Mac OS X) Electron/43.1.0');
    document.body.innerHTML = '<button id="btn" title="提示">+</button><div id="plain"></div>';
    dispose = bindTooltips();
    const btn = document.querySelector('#btn')!;

    hover(btn);
    vi.advanceTimersByTime(500);
    expect((document.querySelector('.mw-tooltip') as HTMLElement).hidden).toBe(false);

    hover(document.querySelector('#plain')!);
    expect((document.querySelector('.mw-tooltip') as HTMLElement).hidden).toBe(true);

    // 再次悬停读 data-mw-tip（title 已转移），仍能显示
    hover(btn);
    vi.advanceTimersByTime(500);
    expect((document.querySelector('.mw-tooltip') as HTMLElement).hidden).toBe(false);
  });

  it('非 macOS：不启用（交给原生气泡）', () => {
    setUserAgent('Mozilla/5.0 (Windows NT 10.0; Win64; x64) Electron/43.1.0');
    document.body.innerHTML = '<button id="btn" title="提示">+</button>';
    dispose = bindTooltips();

    hover(document.querySelector('#btn')!);
    vi.advanceTimersByTime(1000);
    expect(document.querySelector('.mw-tooltip')).toBeNull();
  });

  it('mousedown 立即收起', () => {
    setUserAgent('Mozilla/5.0 (Macintosh; Intel Mac OS X) Electron/43.1.0');
    document.body.innerHTML = '<button id="btn" title="提示">+</button>';
    dispose = bindTooltips();
    const btn = document.querySelector('#btn')!;

    hover(btn);
    vi.advanceTimersByTime(500);
    expect((document.querySelector('.mw-tooltip') as HTMLElement).hidden).toBe(false);
    document.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
    expect((document.querySelector('.mw-tooltip') as HTMLElement).hidden).toBe(true);
  });
});
