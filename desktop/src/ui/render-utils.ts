/**
 * 流式渲染相关的纯逻辑（无 DOM / 无模块级副作用），便于在 node 环境单测。
 *
 * - applyFoldState：toggle 事件委托的纯逻辑核心。
 * - createChatRenderCoalescer：同一调度窗口内合并多次 render，把流式 delta 30/s 的全量重绘降到每帧 ≤1 次。
 */

/** 折叠状态集合（与 state.userUnfoldedTurns / userFoldedTurns 同构）。 */
export interface FoldSets {
  unfolded: Set<string>;
  folded: Set<string>;
}

/**
 * 根据 <details> 的 open 状态更新回合折叠集合。
 * 用户手动展开/折叠后记下选择，下次 render 保留（不被「流式完成自动折叠」覆盖）。
 */
export function applyFoldState(turnId: string, open: boolean, sets: FoldSets): void {
  if (open) {
    sets.unfolded.add(turnId);
    sets.folded.delete(turnId);
  } else {
    sets.folded.add(turnId);
    sets.unfolded.delete(turnId);
  }
}

/**
 * 工具卡片折叠状态集合（与 fold-state.ts 的 setToolFold 对应的纯逻辑镜像）。
 * 单测用；运行时由 fold-state.ts 持久化，这里只提供同构的纯函数便于单测。
 */
export function applyToolFoldState(key: string, open: boolean, map: Map<string, boolean>): void {
  map.set(key, open);
}

/**
 * 创建渲染合并器：同一调度窗口内多次 schedule() 只触发一次 render。
 * scheduler 由调用方注入（renderer 侧用 requestAnimationFrame；单测用同步/可控 scheduler）。
 *
 * 设计要点：scheduled 标志在调度回调真正执行时才清掉，保证窗口内 N 次 schedule → 1 次 render；
 * 回调执行后允许再次 schedule（下一帧再渲染）。
 */
export function createChatRenderCoalescer(
  render: () => void,
  scheduler: (cb: () => void) => void,
): () => void {
  let scheduled = false;
  return function scheduleChatRender(): void {
    if (scheduled) return;
    scheduled = true;
    scheduler(() => {
      scheduled = false;
      render();
    });
  };
}

export interface StreamingPatchTarget {
  sid: string;
  assistantId: string;
}

export interface StreamingPatchCoalescer {
  schedule: (target: StreamingPatchTarget) => void;
  clear: () => void;
}

export function createStreamingPatchCoalescer(
  patch: (target: StreamingPatchTarget) => void,
  scheduler: (cb: () => void) => void,
): StreamingPatchCoalescer {
  let scheduled = false;
  let latest: StreamingPatchTarget | null = null;
  return {
    schedule(target) {
      latest = target;
      if (scheduled) return;
      scheduled = true;
      scheduler(() => {
        scheduled = false;
        const next = latest;
        latest = null;
        if (next) patch(next);
      });
    },
    clear() {
      latest = null;
      scheduled = false;
    },
  };
}
