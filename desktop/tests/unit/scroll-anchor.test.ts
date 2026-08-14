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
