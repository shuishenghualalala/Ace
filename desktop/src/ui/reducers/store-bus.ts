/**
 * 通用 store 接口 + store-bus 中枢。
 *
 * 设计：
 * - 每个 store 是一个 `{ get(): T; set(patch): void; replace(next): void; subscribe(fn): () => void }` 的纯对象
 * - store-bus 提供微任务批处理 + 跨 store 订阅，统一通知时机
 * - 不使用外部状态库（zustand/jotai），保持零依赖 + 可在 tests 中完全 mock
 *
 * 通知策略：
 * - 单个 store.set 触发后，本 store 的订阅者立即同步执行（写时序确定）
 * - 通过 store-bus.subscribe 订阅"任意 store 变化"的事件，节流到下一个微任务批处理
 *   这样渲染层可以在同一帧内合并多个 store 的变化，避免反复重绘
 */

import { NOTIFY_THROTTLE_MS } from '../../shared/constants';

// ---------- 基础 store 接口 ----------

export interface Store<T> {
  /** 当前快照。返回内部可变引用，调用方不应直接修改。 */
  get(): T;
  /** 浅合并 patch + 通知本 store 订阅者 + 推 store-bus 事件。 */
  set(patch: Partial<T>): void;
  /** 深度替换（用于清空、批量重置等）。 */
  replace(next: T): void;
  /** 订阅本 store 变化（同步触发，next/prev 形参为新值）。 */
  subscribe(listener: (next: T, prev: T) => void): () => void;
  /** 唯一名（Symbol），用于 store-bus 事件分发。 */
  readonly name: symbol;
}

/** 创建一个 store：initial 是初始快照。 */
export function createStore<T extends object>(initial: T, name: string): Store<T> {
  let state: T = initial;
  const listeners = new Set<(next: T, prev: T) => void>();
  const sym = Symbol(name);

  return {
    name: sym,
    get: () => state,
    set(patch) {
      const prev = state;
      state = { ...state, ...patch };
      if (prev === state) return;
      for (const l of listeners) l(state, prev);
      storeBus.publish(sym);
    },
    replace(next) {
      const prev = state;
      state = next;
      if (prev === state) return;
      for (const l of listeners) l(state, prev);
      storeBus.publish(sym);
    },
    subscribe(listener) {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
  };
}

// ---------- store-bus 中枢 ----------

type BusListener = (storeName: symbol) => void;

const busListeners = new Set<BusListener>();
let pendingStores = new Set<symbol>();
let flushScheduled = false;
let lastFlush = 0;

function scheduleFlush() {
  if (flushScheduled) return;
  flushScheduled = true;
  const run = (): void => {
    const now = Date.now();
    if (now - lastFlush < NOTIFY_THROTTLE_MS) {
      flushScheduled = false;
      setTimeout(scheduleFlush, NOTIFY_THROTTLE_MS - (now - lastFlush));
      return;
    }
    const stores = Array.from(pendingStores);
    pendingStores = new Set();
    flushScheduled = false;
    lastFlush = Date.now();
    for (const l of busListeners) {
      try {
        for (const s of stores) l(s);
      } catch (err) {
        console.warn('[store-bus] listener threw:', (err as Error).message);
      }
    }
  };
  // queueMicrotask 在浏览器 / Node 18+ 都支持
  queueMicrotask(run);
}

export const storeBus = {
  /** 任意 store 在 set/replace 后调用，通知总线订阅者。 */
  publish(storeName: symbol) {
    pendingStores.add(storeName);
    scheduleFlush();
  },
  /** 订阅"任意 store 变化"，回调在下一个微任务批处理中被调用。 */
  subscribe(listener: BusListener): () => void {
    busListeners.add(listener);
    return () => {
      busListeners.delete(listener);
    };
  },
  /** 测试钩子：清空所有订阅与待处理事件。 */
  __resetForTest() {
    busListeners.clear();
    pendingStores = new Set();
    flushScheduled = false;
    lastFlush = 0;
  },
};