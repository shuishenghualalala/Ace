/**
 * 自绘 tooltip。
 *
 * 背景：Electron 38+ 在 macOS 上原生 title 气泡基本不再显示（上游 bug
 * https://github.com/electron/electron/issues/49843 ，37 是最后正常版本；
 * Windows / Linux 不受影响）。本组件用全局事件委托给 [title] 元素补回悬停提示，
 * 仅在 macOS 启用；其他平台继续走原生气泡。
 *
 * 首次悬停时把 title 转移到 data-mw-tip 并移除 title 属性：原生气泡在 macOS 上
 * 偶发还会弹一次，不挪走会与自绘气泡叠加。aria-label 不受影响。
 */

import { setRuntimeStyle } from './runtime-style';

const SHOW_DELAY_MS = 450;
const VIEWPORT_MARGIN = 8;
const ANCHOR_GAP = 6;
const TIP_SELECTOR = '[title], [data-mw-tip]';

export function bindTooltips(): () => void {
  if (!/Mac/.test(navigator.userAgent)) return () => {};

  const controller = new AbortController();
  const { signal } = controller;
  let anchor: Element | null = null;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let bubble: HTMLDivElement | null = null;

  const clearTimer = (): void => {
    if (timer != null) {
      clearTimeout(timer);
      timer = null;
    }
  };

  const hide = (): void => {
    clearTimer();
    anchor = null;
    if (bubble) bubble.hidden = true;
  };

  const positionBubble = (target: Element): void => {
    if (!bubble) return;
    const rect = target.getBoundingClientRect();
    // 先放到屏外量尺寸，再落位，避免闪烁。
    setRuntimeStyle(bubble, 'left', '-9999px');
    setRuntimeStyle(bubble, 'top', '0px');
    const bw = bubble.offsetWidth;
    const bh = bubble.offsetHeight;
    const left = Math.min(
      Math.max(rect.left + rect.width / 2 - bw / 2, VIEWPORT_MARGIN),
      Math.max(window.innerWidth - bw - VIEWPORT_MARGIN, VIEWPORT_MARGIN),
    );
    let top = rect.bottom + ANCHOR_GAP;
    if (top + bh > window.innerHeight - VIEWPORT_MARGIN) {
      top = Math.max(rect.top - bh - ANCHOR_GAP, VIEWPORT_MARGIN);
    }
    setRuntimeStyle(bubble, 'left', `${Math.round(left)}px`);
    setRuntimeStyle(bubble, 'top', `${Math.round(top)}px`);
  };

  const show = (target: Element): void => {
    // title 转移到 data-mw-tip（只挪一次；后续悬停直接读 data-mw-tip）。
    const title = target.getAttribute('title');
    if (title != null) {
      target.setAttribute('data-mw-tip', title);
      target.removeAttribute('title');
    }
    const text = target.getAttribute('data-mw-tip');
    if (!text) return;
    if (!bubble) {
      bubble = document.createElement('div');
      bubble.className = 'mw-tooltip';
      bubble.setAttribute('role', 'tooltip');
      document.body.appendChild(bubble);
    }
    bubble.textContent = text;
    bubble.hidden = false;
    positionBubble(target);
  };

  const schedule = (target: Element): void => {
    if (target === anchor) return;
    hide();
    anchor = target;
    timer = setTimeout(() => {
      timer = null;
      if (anchor) show(anchor);
    }, SHOW_DELAY_MS);
  };

  document.addEventListener('mouseover', (event) => {
    const el = event.target instanceof Element ? event.target.closest(TIP_SELECTOR) : null;
    if (el) schedule(el);
    else hide();
  }, { signal });
  // 指针移出窗口（relatedTarget 为 null）时 mouseover 不会再触发，单独兜底。
  document.addEventListener('mouseout', (event) => {
    if (!event.relatedTarget) hide();
  }, { signal });
  // 点击 / 滚动 / 滚轮立即收起，避免气泡挂在已变化的 UI 上。
  document.addEventListener('mousedown', hide, { signal, capture: true });
  document.addEventListener('wheel', hide, { signal, capture: true });
  document.addEventListener('scroll', hide, { signal, capture: true });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') hide();
  }, { signal });
  // 键盘可达性：聚焦也出提示。
  document.addEventListener('focusin', (event) => {
    const el = event.target instanceof Element ? event.target.closest(TIP_SELECTOR) : null;
    if (el) schedule(el);
  }, { signal });
  document.addEventListener('focusout', hide, { signal });
  window.addEventListener('blur', hide, { signal });
  window.addEventListener('resize', hide, { signal });

  return () => {
    controller.abort();
    clearTimer();
    bubble?.remove();
    bubble = null;
    anchor = null;
  };
}
