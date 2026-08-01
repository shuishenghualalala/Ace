/**
 * happy-dom 20 + vitest 1.6 的组合下 window.localStorage 退化成普通 Object
 * （无 getItem/setItem/clear 等 Storage 方法），导致直接调用 localStorage.clear()
 * 的测试在 beforeEach 全部炸掉，源码侧虽有 try/catch 兜底但持久化路径在测试里
 * 完全失真。这里用 Map 实现最小 Storage 接口垫片，只对缺失方法补齐，不影响
 * 真实浏览器/Electron 环境。
 */

function createMemoryStorage(): Storage {
  const data = new Map<string, string>();
  return {
    get length() {
      return data.size;
    },
    clear: () => data.clear(),
    getItem: (key: string) => (data.has(key) ? data.get(key)! : null),
    key: (index: number) => Array.from(data.keys())[index] ?? null,
    removeItem: (key: string) => {
      data.delete(key);
    },
    setItem: (key: string, value: string) => {
      data.set(key, String(value));
    },
  } as Storage;
}

function ensureStorage(target: object, prop: 'localStorage' | 'sessionStorage'): void {
  try {
    const current = (target as Record<string, unknown>)[prop];
    if (current && typeof (current as Storage).getItem === 'function') return;
    Object.defineProperty(target, prop, {
      value: createMemoryStorage(),
      configurable: true,
      writable: true,
    });
  } catch {
    // 环境不提供可定义的全局对象时静默跳过（如纯 node 环境无 window）。
  }
}

if (typeof window !== 'undefined') {
  ensureStorage(window, 'localStorage');
  ensureStorage(window, 'sessionStorage');
}
