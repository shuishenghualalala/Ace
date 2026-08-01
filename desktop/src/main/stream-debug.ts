/**
 * 主进程流式诊断（与 renderer stream-debug.ts 对齐，用环境变量开关）。
 *
 * 启动 desktop 前设置：MM_DEBUG_STREAM=1
 */

const RING_MAX = 80;

export interface MainStreamDebugEntry {
  ts: number;
  event: string;
  detail?: Record<string, unknown>;
}

const ring: MainStreamDebugEntry[] = [];

export function isMainStreamDebugEnabled(): boolean {
  return process.env.MM_DEBUG_STREAM === '1' || process.env.MM_DEBUG_STREAM === 'true';
}

export function logMainStream(event: string, detail?: Record<string, unknown>): void {
  if (!isMainStreamDebugEnabled()) return;
  const entry: MainStreamDebugEntry = detail ? { ts: Date.now(), event, detail } : { ts: Date.now(), event };
  ring.push(entry);
  if (ring.length > RING_MAX) ring.shift();
  const tag = `[mm:stream][ws-main] ${event}`;
  if (detail && Object.keys(detail).length > 0) {
    console.info(tag, detail);
  } else {
    console.info(tag);
  }
}

export function dumpMainStreamDebugLog(): MainStreamDebugEntry[] {
  return [...ring];
}
