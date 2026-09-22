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

/** 工厂：注入 transport / now，便于单测去抖行为。 */
export function createRendererErrorReporter(
  transport: RendererErrorTransport,
  now: () => number = Date.now,
): RendererErrorReporter {
  const lastSentAt = new Map<string, number>();
  return {
    report(source, err, context) {
      const message = String(
        err instanceof Error ? err.message : typeof err === 'string' ? err : JSON.stringify(err),
      ).slice(0, MESSAGE_SLICE) || 'unknown error';
      const stack = err instanceof Error && err.stack ? err.stack.slice(0, STACK_SLICE) : undefined;
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
