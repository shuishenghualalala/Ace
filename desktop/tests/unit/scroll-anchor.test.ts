/**
 * scroll-anchor 单测：stickyBottom / wheel disarm / 程序滚动 guard。
 * @vitest-environment happy-dom
 */
import { describe, it, expect } from 'vitest';
import { attachScrollAnchor } from '../../src/ui/features/scroll-anchor';

function makeContainer(): HTMLElement {
  const el = document.createElement('div');
  // happy-dom 默认 clientHeight/scrollHeight 是 0；手动设值便于测试。
  Object.defineProperty(el, 'clientHeight', { configurable: true, value: 600 });
  Object.defineProperty(el, 'scrollHeight', { configurable: true, value: 1200 });
  return el;
}

describe('attachScrollAnchor', () => {
  it('上滑后迟到的底部 scroll 不恢复跟随，主动滚回底部才恢复', () => {
    const el = makeContainer();
    el.scrollTop = 600;
    const anchor = attachScrollAnchor(el);
    el.dispatchEvent(new WheelEvent('wheel', { deltaY: -100 }));
    el.dispatchEvent(new Event('scroll'));
    expect(anchor.isStickyBottom()).toBe(false);
    el.scrollTop = 300;
    el.dispatchEvent(new Event('scroll'));
    Object.defineProperty(el, 'scrollHeight', { configurable: true, value: 1400 });
    anchor.pinToBottomIfSticky();
    expect(el.scrollTop).toBe(300);
    el.scrollTop = 800;
    el.dispatchEvent(new Event('scroll'));
    expect(anchor.isStickyBottom()).toBe(true);
    anchor.dispose();
  });

  it('程序滚动尚未回调时拖动滚动条向上，且内容增长，不吞掉上滑', () => {
    const el = makeContainer();
    const anchor = attachScrollAnchor(el);
    anchor.pinToBottomIfSticky();
    Object.defineProperty(el, 'scrollHeight', { configurable: true, value: 1600 });
    el.scrollTop = 200;
    el.dispatchEvent(new Event('scroll'));
    anchor.pinToBottomIfSticky();
    expect(el.scrollTop).toBe(200);
    expect(anchor.isStickyBottom()).toBe(false);
    anchor.dispose();
  });

  it('初始 stickyBottom=true，pinToBottomIfSticky 会滚到底', () => {
    const el = makeContainer();
    const anchor = attachScrollAnchor(el);
    expect(anchor.isStickyBottom()).toBe(true);
    anchor.pinToBottomIfSticky();
    expect(el.scrollTop).toBe(el.scrollHeight);
    anchor.dispose();
  });

  it('wheel-up disarm stickyBottom，pinToBottomIfSticky 不再追底', () => {
    const el = makeContainer();
    const anchor = attachScrollAnchor(el);
    el.scrollTop = 0; // 用户在顶部
    el.dispatchEvent(new WheelEvent('wheel', { deltaY: -100 }));
    expect(anchor.isStickyBottom()).toBe(false);
    const before = el.scrollTop;
    anchor.pinToBottomIfSticky();
    expect(el.scrollTop).toBe(before); // 没追底
    anchor.dispose();
  });

  it('jumpToBottom 强制重置 sticky 并滚到底', () => {
    const el = makeContainer();
    const anchor = attachScrollAnchor(el);
    // 先 disarm
    el.dispatchEvent(new WheelEvent('wheel', { deltaY: -100 }));
    expect(anchor.isStickyBottom()).toBe(false);
    anchor.jumpToBottom();
    expect(anchor.isStickyBottom()).toBe(true);
    expect(el.scrollTop).toBe(el.scrollHeight);
    expect(() => anchor.dispose()).not.toThrow();
  });

  it('touchmove 手指下滑（clientY 增大）disarm，手指上滑不 disarm', () => {
    const el = makeContainer();
    const anchor = attachScrollAnchor(el);
    // touchstart 先记下起点 Y=500
    el.dispatchEvent(new TouchEvent('touchstart', { touches: [{ clientY: 500 } as unknown as Touch] }));
    // 手指上滑（clientY 500→400，想看下面）：不应 disarm
    el.dispatchEvent(new TouchEvent('touchmove', { touches: [{ clientY: 400 } as unknown as Touch] }));
    expect(anchor.isStickyBottom()).toBe(true);
    // 手指下滑（clientY 400→500，想看上面）：应 disarm
    el.dispatchEvent(new TouchEvent('touchmove', { touches: [{ clientY: 500 } as unknown as Touch] }));
    expect(anchor.isStickyBottom()).toBe(false);
    anchor.dispose();
  });

  it('disarm() 显式 disarm', () => {
    const el = makeContainer();
    const anchor = attachScrollAnchor(el);
    anchor.disarm();
    expect(anchor.isStickyBottom()).toBe(false);
    anchor.dispose();
  });
});
