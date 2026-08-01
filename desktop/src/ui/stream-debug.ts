/**
 * 流式对话诊断（仅开发排查用）。
 *
 * 开启方式（renderer）：
 *   localStorage.setItem('mm:debug-stream', '1'); location.reload();
 * 关闭：
 *   localStorage.removeItem('mm:debug-stream'); location.reload();
 *
 * 或在 DevTools Console：
 *   __mmStreamDebug.enable() / __mmStreamDebug.disable() / __mmStreamDebug.dump()
 *
 * 主进程（Electron main）额外支持环境变量 MM_DEBUG_STREAM=1。
 */

const STORAGE_KEY = 'mm:debug-stream';
const RING_MAX = 80;

export type StreamDebugScope =
  | 'ws-renderer'
  | 'ws-main'
  | 'apply-chunk'
  | 'dispatch'
  | 'history'
  | 'gate'
  | 'render'
  | 'watchdog'
  | 'gateway-seq';

export interface StreamDebugEntry {
  ts: number;
  scope: StreamDebugScope;
  event: string;
  detail?: Record<string, unknown>;
}

const ring: StreamDebugEntry[] = [];

function pushEntry(entry: StreamDebugEntry): void {
  ring.push(entry);
  if (ring.length > RING_MAX) ring.shift();
}

/** 是否开启流式诊断（renderer）。 */
export function isStreamDebugEnabled(): boolean {
  if (typeof window === 'undefined') return false;
  try {
    return window.localStorage.getItem(STORAGE_KEY) === '1';
  } catch {
    return false;
  }
}

/** 结构化日志 + 环形缓冲；未开启时 no-op。 */
export function logStream(
  scope: StreamDebugScope,
  event: string,
  detail?: Record<string, unknown>,
): void {
  if (!isStreamDebugEnabled()) return;
  const entry: StreamDebugEntry = detail ? { ts: Date.now(), scope, event, detail } : { ts: Date.now(), scope, event };
  pushEntry(entry);
  const tag = `[mm:stream][${scope}] ${event}`;
  if (detail && Object.keys(detail).length > 0) {
    console.info(tag, detail);
  } else {
    console.info(tag);
  }
}

/** 供 DevTools 复制最近事件序列。 */
export function dumpStreamDebugLog(): StreamDebugEntry[] {
  return [...ring];
}

/** 挂载到 window，便于 Console 操作。 */
export function installStreamDebugGlobal(): void {
  if (typeof window === 'undefined') return;
  const api = {
    enable: () => {
      window.localStorage.setItem(STORAGE_KEY, '1');
      console.info('[mm:stream] 已开启；请 reload 或继续发消息观察日志');
    },
    disable: () => {
      window.localStorage.removeItem(STORAGE_KEY);
      console.info('[mm:stream] 已关闭');
    },
    isEnabled: () => isStreamDebugEnabled(),
    dump: () => {
      const copy = dumpStreamDebugLog();
      console.table(copy.map((e) => ({
        time: new Date(e.ts).toISOString(),
        scope: e.scope,
        event: e.event,
        ...(e.detail ?? {}),
      })));
      return copy;
    },
    clear: () => {
      ring.length = 0;
      console.info('[mm:stream] ring buffer cleared');
    },
  };
  (window as unknown as { __mmStreamDebug?: typeof api }).__mmStreamDebug = api;
}
