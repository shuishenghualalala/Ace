/**
 * 对话滚动锚定：stickyBottom 模型 + 程序滚动 guard + wheel/touch disarm。
 *
 * 核心契约（与 Crew 一致）：
 *  - 故意不做「流式追底」：一旦回合在飞，视口停在用户离开的位置，不追流。
 *  - 只在三个时机跳到底部：会话切换、空→非空、用户提交新消息（runStart）。
 *  - wheel-up / 手指下滑（想往上看）立即 disarm stickyBottom；用户滚回底部时 re-arm。
 *  - 程序滚动 guard：自己写的 scrollTop 会触发 scroll 事件，要避免把它误读成用户上滑。
 *  - 容器 resize（窗口拖动/composer 折叠）时若仍 stickyBottom，重新贴底，避免留白。
 */

/** 距底部 ≤ 此阈值（px）视为「在底部」。 */
const AT_BOTTOM_THRESHOLD = 4;
/** 触屏方向判断的位移阈值（px），避免微小抖动误 disarm。 */
const TOUCH_DIRECTION_THRESHOLD = 2;

export interface ScrollAnchor {
  /** 程序跳到底部：重置 stickyBottom=true 并立即滚到底。用于提交/切会话/空→非空。 */
  jumpToBottom(): void;
  /** 软钉底：仅在 stickyBottom 已 armed 时滚到底；未 armed 则不动。用于流式增量。 */
  pinToBottomIfSticky(): void;
  /** 当前是否处于 stickyBottom（停在底部）。 */
  isStickyBottom(): boolean;
  /** 显式 disarm（外部知道用户开始操作时调用）。 */
  disarm(): void;
  /** 销毁：移除所有监听。 */
  dispose(): void;
}

/**
 * 给一个滚动容器装上锚定逻辑。返回 ScrollAnchor 接口供外部控制。
 *
 * 用法：
 *   const anchor = attachScrollAnchor(containerEl);
 *   anchor.jumpToBottom();            // 用户提交新消息
 *   // 流式增量到来时：
 *   anchor.pinToBottomIfSticky();     // 仅在用户没上滑时追底
 *   // 销毁：
 *   anchor.dispose();
 */
export function attachScrollAnchor(container: HTMLElement): ScrollAnchor {
  // stickyBottom：用户是否「停在底部」。内容增长时是否跟随取决于此。
  let stickyBottom = true;
  // 程序滚动 guard：自己写 scrollTop 会触发 scroll 事件，要避免把它误读成用户上滑。
  // 用计数器而不是 boolean，因为同一帧内多次写 scrollTop 只触发一次 scroll 事件，
  // 计数器 > 1 永远不递减会导致后续真实用户上滑被吞。
  let programmaticScrollPending = 0;
  // 上一帧的 scrollTop / scrollHeight / clientHeight，用于判定「scrollTop 减小是否真的
  // 是用户上滑」——内容增长 / 视口变化也会让 scrollTop 变化，要排除这些情况。
  let lastTop = container.scrollTop;
  let lastHeight = container.scrollHeight;
  let lastClientHeight = container.clientHeight;

  const isAtBottom = (): boolean =>
    container.scrollHeight - (container.scrollTop + container.clientHeight) <= AT_BOTTOM_THRESHOLD;

  const scrollToBottom = (): void => {
    container.scrollTop = container.scrollHeight;
  };

  // ---------- 事件处理 ----------

  const onScroll = (): void => {
    const top = container.scrollTop;

    // 程序自己写的 scrollTop 触发的 scroll 事件：不当成用户操作。
    if (programmaticScrollPending > 0) {
      programmaticScrollPending -= 1;
      lastTop = top;
      lastHeight = container.scrollHeight;
      lastClientHeight = container.clientHeight;
      // 始终 re-arm——stickyBottom 应在 clamp 竞态中保持。
      stickyBottom = true;
      return;
    }

    // 仅当「内容高度和视口高度都稳定，且 scrollTop 真的减小」时才 disarm。
    // 单纯 `top < lastTop` 不安全：虚拟化测量、流式 markdown、composer resize、
    // 窗口 resize 都可能让 scrollTop 作为布局副作用变化。
    const heightGrew = container.scrollHeight > lastHeight;
    const clientHeightChanged = Math.abs(container.clientHeight - lastClientHeight) > 1;
    if (!heightGrew && !clientHeightChanged && top + 1 < lastTop) {
      stickyBottom = false;
    }

    lastTop = top;
    lastHeight = container.scrollHeight;
    lastClientHeight = container.clientHeight;

    // 用户滚回底部 → re-arm
    if (isAtBottom()) {
      stickyBottom = true;
    }
  };

  // wheel-up 立即 disarm——比 scroll 事件更早、更可靠地捕捉「用户想往上看」。
  // deltaY < 0 = 滚轮向上滚 = 内容向上移 = 想看上面。
  const onWheel = (e: WheelEvent): void => {
    if (e.deltaY < 0) {
      stickyBottom = false;
      programmaticScrollPending = 0;
    }
  };

  // touchmove 按方向 disarm：手指下滑（clientY 增大）= 内容向上移 = 想看上面 → disarm。
  // 手指上滑（clientY 减小）= 想看下面 → 不 disarm（保持 sticky，让内容继续跟随）。
  // 这与 wheel 的「 deltaY < 0 才 disarm 」语义对齐，避免用户滑到底时误 disarm。
  let lastTouchY = 0;
  const onTouchStart = (e: TouchEvent): void => {
    lastTouchY = e.touches[0]?.clientY ?? lastTouchY;
  };
  const onTouchMove = (e: TouchEvent): void => {
    const y = e.touches[0]?.clientY;
    if (y === undefined) return;
    // 手指下滑超过阈值 → 用户想把内容往上推、看上面 → disarm。
    if (y > lastTouchY + TOUCH_DIRECTION_THRESHOLD) {
      stickyBottom = false;
      programmaticScrollPending = 0;
    }
    lastTouchY = y;
  };

  // 容器 resize（窗口拖大/composer 折叠导致 clientHeight 变化）：stickyBottom 时重新贴底。
  // 否则用户原本在底部、窗口变高后会留白。用 ResizeObserver 监听容器自身高度变化即可。
  // （ResizeObserver 实例在对外接口定义之后才创建，确保 onResize 引用的 pinToBottomIfSticky 已就绪。）
  const onResize = (): void => {
    if (stickyBottom) {
      pinToBottomIfSticky();
    }
  };
  let resizeObserver: ResizeObserver | null = null;

  container.addEventListener('scroll', onScroll, { passive: true });
  container.addEventListener('wheel', onWheel, { passive: true });
  container.addEventListener('touchstart', onTouchStart, { passive: true });
  container.addEventListener('touchmove', onTouchMove, { passive: true });

  // ---------- 对外接口 ----------

  const jumpToBottom = (): void => {
    stickyBottom = true;
    // 直接滚——这是用户提交/切会话的强制跳底，不需要 guard。
    scrollToBottom();
    // 下一帧再滚一次，防止 React/DOM 异步挂载导致首帧没滚到底。
    requestAnimationFrame(() => {
      if (stickyBottom) scrollToBottom();
    });
  };

  const pinToBottomIfSticky = (): void => {
    if (!stickyBottom) return;
    // 已在底部：写 scrollTop 是 no-op，浏览器不触发 scroll 事件，
    // 此时 arm guard 会永久卡住，所以直接刷新 tracker 返回。
    if (isAtBottom()) {
      lastTop = container.scrollTop;
      lastHeight = container.scrollHeight;
      lastClientHeight = container.clientHeight;
      return;
    }
    // 跨 scroll 事件保持 guard：设 1 而非累加，避免多次合并写入导致 guard 永不递减。
    programmaticScrollPending = 1;
    scrollToBottom();
    lastTop = container.scrollTop;
    lastHeight = container.scrollHeight;
    lastClientHeight = container.clientHeight;
  };

  const isStickyBottom = (): boolean => stickyBottom;

  const disarm = (): void => {
    stickyBottom = false;
    programmaticScrollPending = 0;
  };

  // ResizeObserver 在对外接口定义之后创建，确保 onResize 引用的 pinToBottomIfSticky 已就绪。
  if (typeof ResizeObserver !== 'undefined') {
    resizeObserver = new ResizeObserver(onResize);
    resizeObserver.observe(container);
  }

  const dispose = (): void => {
    container.removeEventListener('scroll', onScroll);
    container.removeEventListener('wheel', onWheel);
    container.removeEventListener('touchstart', onTouchStart);
    container.removeEventListener('touchmove', onTouchMove);
    resizeObserver?.disconnect();
  };

  return { jumpToBottom, pinToBottomIfSticky, isStickyBottom, disarm, dispose };
}
