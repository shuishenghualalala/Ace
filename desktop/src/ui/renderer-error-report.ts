/**
 * 渲染层错误上报：window.onerror / unhandledrejection / 渲染隔离兜底的统一入口。
 *
 * - 同类错误 60s 去抖（风暴抑制），console 始终可见（DevTools 排查不受去抖影响）
 * - 经 Crew.reportRendererError IPC 落主进程 logs/renderer-errors.log（主进程侧轮转）
 * - 模块无顶层副作用、transport 惰性获取：render-utils 等纯逻辑模块可安全引用（node 单测友好）
 */

export type RendererErrorSource =
  | 'window.onerror'
  | 'unhandledrejection'
  | 'render'
  | 'streaming-patch'
  | 'apply-chunk'
  | 'ws-frame'
  | 'watchdog';

export interface RendererErrorReport {
  source: RendererErrorSource;
  message: string;
  stack?: string;
  context?: Record<string, unknown>;
}

export type RendererErrorTransport = (report: RendererErrorReport) => void;

const DEDUPE_WINDOW_MS = 60_000;
const STACK_SLICE = 2_000;
const MESSAGE_SLICE = 500;

export interface RendererErrorReporter {
  report(source: RendererErrorSource, err: unknown, context?: Record<string, unknown>): void;
  /** 单测/重置：清空去抖窗口。 */
  reset(): void;
}

/**
 * 描述任意值的安全字符串化：循环引用、BigInt、抛错的 getter / 代理都不得让它自己抛。
 * 上报通道是「绝不把异常还给调用方」的最后一环，取值失败只能降级为类型标签。
 * 也用于 sig 计算的兜底摘要（见 conversation-renderer）。
 */
export function safeStringify(value: unknown): string {
  if (typeof value === 'string') return value;
  try {
    const seen = new WeakSet<object>();
    const json = JSON.stringify(value, (_key, current: unknown) => {
      if (typeof current === 'bigint') return `${current}n`;
      if (typeof current === 'object' && current !== null) {
        if (seen.has(current)) return '[circular]';
        seen.add(current);
      }
      return current;
    });
    if (typeof json === 'string') return json;
  } catch {
    /* 取值 / 序列化失败 → 退回类型标签 */
  }
  try {
    return Object.prototype.toString.call(value);
  } catch {
    return '[unprintable]';
  }
}

/** 提取上报文案：任何取值异常都降级为 unknown error，绝不抛出。 */
function describeError(err: unknown): string {
  try {
    const raw = err instanceof Error ? err.message : safeStringify(err);
    return String(raw).slice(0, MESSAGE_SLICE) || 'unknown error';
  } catch {
    return 'unknown error';
  }
}

/** 工厂：注入 transport / now，便于单测去抖行为。 */
export function createRendererErrorReporter(
  transport: RendererErrorTransport,
  now: () => number = Date.now,
): RendererErrorReporter {
  const lastSentAt = new Map<string, number>();
  return {
    report(source, err, context) {
      const message = describeError(err);
      const stack = err instanceof Error && typeof err.stack === 'string'
        ? err.stack.slice(0, STACK_SLICE)
        : undefined;
      console.error(`[renderer:${source}]`, err);
      const key = `${source}|${message}`;
      const nowTs = now();
      const last = lastSentAt.get(key);
      if (last !== undefined && nowTs - last < DEDUPE_WINDOW_MS) return;
      lastSentAt.set(key, nowTs);
      try {
        transport({ source, message, ...(stack ? { stack } : {}), ...(context ? { context } : {}) });
      } catch {
        /* 上报通道自身失败不再上报（避免自激） */
      }
    },
    reset() {
      lastSentAt.clear();
    },
  };
}

function defaultTransport(report: RendererErrorReport): void {
  window.Crew?.reportRendererError?.(report);
}

const defaultReporter = createRendererErrorReporter(defaultTransport);

/** 统一上报入口（去抖 + console + IPC）。 */
export function reportRendererError(
  source: RendererErrorSource,
  err: unknown,
  context?: Record<string, unknown>,
): void {
  defaultReporter.report(source, err, context);
}

/** bootstrap 最早处安装：捕获逃逸到 window 的同步异常与未处理 Promise 拒绝。 */
export function installGlobalErrorReporting(): void {
  if (typeof window === 'undefined') return;
  window.addEventListener('error', (event) => {
    reportRendererError('window.onerror', event.error ?? event.message, {
      filename: event.filename,
      lineno: event.lineno,
      colno: event.colno,
    });
  });
  window.addEventListener('unhandledrejection', (event) => {
    reportRendererError('unhandledrejection', event.reason);
  });
}

/** 单测入口：重置默认 reporter 的去抖窗口。 */
export function _resetRendererErrorReporterForTests(): void {
  defaultReporter.reset();
}
