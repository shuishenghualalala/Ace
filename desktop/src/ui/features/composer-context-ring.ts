/**
 * Composer 上下文占比圆环：模型与发送按钮之间，实时展示当前会话 token 占用。
 * createContextRingController 为实例级实现：主对话与 Wiki 问答面板各持一个，
 * 各自的 getSessionId 决定跟踪哪个会话，互不干扰。
 */

import { backendApi } from '../backend-client';
import { $, state } from '../state';
import { resolveSessionModelWindow } from './session-model';

const RING_RADIUS = 9;
const RING_CIRC = 2 * Math.PI * RING_RADIUS;

/**
 * 事件驱动刷新的最小间隔：流式输出期间 messages:changed 逐 chunk 触发，
 * 而用量百分比每秒只走约 0.02%，2s 节流在体感上仍是实时的。
 */
const EVENT_REFRESH_MIN_INTERVAL_MS = 2000;

/** 将 token 数格式化为 K/M 缩写（与 WorkBuddy 提示风格接近）。 */
function formatTokenCount(n: number): string {
  const v = Math.max(0, Math.round(n));
  if (v >= 1_000_000) return `${(v / 1000 / 1000).toFixed(1)}M`;
  if (v >= 1000) return `${(v / 1000).toFixed(1)}K`;
  return String(v);
}

export interface ContextRingElements {
  btn: HTMLButtonElement;
  pct: HTMLElement;
  progress: SVGCircleElement;
}

export interface ContextRingController {
  /** 拉取并刷新本实例会话的上下文圆环。 */
  refresh(): void;
  dispose(): void;
}

export interface ContextRingControllerOptions {
  /** 本实例跟踪的会话（主对话 = 全局活跃会话；Wiki = 内嵌会话）。 */
  getSessionId: () => string | null;
  /** 上下文窗口分母（按本实例会话的绑定模型取）。 */
  resolveWindow: () => number;
  /** 圆环是否可见（所在 tab 激活）。不可见时不拉取——看不见的圆环不白请求。 */
  isActive?: () => boolean;
}

export function createContextRingController(
  els: ContextRingElements,
  opts: ContextRingControllerOptions,
): ContextRingController {
  const controller = new AbortController();
  const { signal } = controller;
  let lastSessionId: string | null = null;
  let inflight: Promise<void> | null = null;
  let lastFetchAt = 0;
  let trailingTimer: number | null = null;
  // 最近一次最终请求视图的 prompt token 数；来源可能是 Provider 或本地请求视图计数。
  let latestPromptTokens: number | null = null;
  let latestPromptSource: 'provider' | 'request_view' = 'provider';

  const setRingProgress = (ratio: number): void => {
    const clamped = Math.max(0, Math.min(1, ratio));
    els.progress.setAttribute('stroke-dasharray', String(RING_CIRC));
    els.progress.setAttribute('stroke-dashoffset', String(RING_CIRC * (1 - clamped)));
    const pct = Math.round(clamped * 100);
    els.pct.textContent = pct >= 100 ? '满' : `${pct}%`;
    els.btn.classList.remove('is-warn', 'is-critical');
    if (clamped >= 0.9) els.btn.classList.add('is-critical');
    else if (clamped >= 0.7) els.btn.classList.add('is-warn');
  };

  const setRingTooltip = (used: number, max: number, ratio: number, source: 'provider' | 'request_view'): void => {
    const pct = (ratio * 100).toFixed(1);
    const sourceLabel = source === 'provider' ? '实际用量' : '本地计算';
    els.btn.title = `${pct}% · ${formatTokenCount(used)} / ${formatTokenCount(max)} 上下文 · ${sourceLabel}`;
    els.btn.setAttribute('aria-label', els.btn.title);
  };

  const setRingUsage = (used: number, max: number, source: 'provider' | 'request_view'): void => {
    const ratio = max > 0 ? used / max : 0;
    setRingProgress(ratio);
    setRingTooltip(used, max, ratio, source);
  };

  const setRingUnavailable = (max: number, warning?: string): void => {
    setRingProgress(0);
    els.pct.textContent = '?';
    els.btn.classList.remove('is-warn', 'is-critical');
    els.btn.title = warning || `上下文用量不可用 · 窗口 ${formatTokenCount(max)}`;
    els.btn.setAttribute('aria-label', els.btn.title);
  };

  const hideRing = (): void => {
    els.btn.hidden = true;
    setRingProgress(0);
    els.btn.classList.remove('is-warn', 'is-critical');
    els.btn.title = '上下文占用';
    els.pct.textContent = '0%';
  };

  const refresh = (): void => {
    if (opts.isActive && !opts.isActive()) return;
    const sid = opts.getSessionId();
    if (!sid) {
      hideRing();
      lastSessionId = null;
      return;
    }

    els.btn.hidden = false;
    if (sid === lastSessionId && inflight) return;

    const run = async (): Promise<void> => {
      try {
        const ctx = await backendApi.sessionContext(sid);
        if (opts.getSessionId() !== sid) return;
        lastSessionId = sid;
        // 分母用会话绑定模型的窗口；网关返回的 max_tokens 是全局窗口，直接用会与 Inspector 口径不一致。
        const max = opts.resolveWindow();
        // Provider 实际 prompt_tokens 优先；否则使用后端按最终请求视图计算的值。
        if (latestPromptTokens !== null) {
          setRingUsage(latestPromptTokens, max, latestPromptSource);
        } else if (ctx.available && typeof ctx.used_tokens === 'number') {
          const source = ctx.source === 'request_view' ? 'request_view' : 'provider';
          setRingUsage(ctx.used_tokens, max, source);
        } else {
          setRingUnavailable(max, ctx.warning);
        }
      } catch {
        if (opts.getSessionId() !== sid) return;
        setRingProgress(0);
        els.btn.title = '上下文占用（暂无法获取）';
      }
    };

    lastFetchAt = Date.now();
    inflight = run().finally(() => {
      inflight = null;
    });
  };

  /** 事件驱动刷新：2s 最小间隔 + 尾随补一次，保证流式结束后拿到最终用量。 */
  const throttledRefresh = (): void => {
    const elapsed = Date.now() - lastFetchAt;
    if (elapsed >= EVENT_REFRESH_MIN_INTERVAL_MS) {
      refresh();
    } else if (trailingTimer === null) {
      trailingTimer = window.setTimeout(() => {
        trailingTimer = null;
        throttledRefresh();
      }, EVENT_REFRESH_MIN_INTERVAL_MS - elapsed);
    }
  };

  /** 事件 detail 里带 sessionId 时，只响应本实例会话的变化。 */
  const isOwnSessionEvent = (event: Event): boolean => {
    const sid = (event as CustomEvent<{ sessionId?: string }>).detail?.sessionId;
    return !sid || sid === opts.getSessionId();
  };

  window.addEventListener('session:changed', () => {
    latestPromptTokens = null;
    latestPromptSource = 'provider';
    lastSessionId = null;
    refresh();
  }, { signal });
  window.addEventListener('messages:changed', (event) => {
    if (!isOwnSessionEvent(event)) return;
    throttledRefresh();
  }, { signal });
  window.addEventListener('session:model-changed', (event) => {
    if (!isOwnSessionEvent(event)) return;
    latestPromptTokens = null;
    latestPromptSource = 'provider';
    lastSessionId = null;
    refresh();
  }, { signal });
  window.addEventListener('context:usage', (event) => {
    const detail = (event as CustomEvent<{
      sessionId?: string;
      promptTokens?: number;
      promptTokensSource?: 'provider' | 'request_view';
    }>).detail;
    if (detail?.sessionId !== opts.getSessionId()) return;
    if (typeof detail.promptTokens !== 'number' || !Number.isFinite(detail.promptTokens)) return;
    latestPromptTokens = Math.max(0, Math.round(detail.promptTokens));
    latestPromptSource = detail.promptTokensSource === 'request_view' ? 'request_view' : 'provider';
    setRingUsage(latestPromptTokens, opts.resolveWindow(), latestPromptSource);
  }, { signal });
  window.addEventListener('context:usage-cleared', (event) => {
    if (!isOwnSessionEvent(event)) return;
    latestPromptTokens = null;
    latestPromptSource = 'provider';
    setRingUnavailable(opts.resolveWindow(), '本轮尚未收到 Provider prompt_tokens，当前上下文用量不可用');
  }, { signal });

  return {
    refresh,
    dispose() {
      if (trailingTimer !== null) window.clearTimeout(trailingTimer);
      controller.abort();
    },
  };
}

/** 主对话圆环实例（bindComposerContextRing 创建；元素由 composer-context-view 生成）。 */
let mainRingController: ContextRingController | null = null;

export function bindComposerContextRing(): void {
  const btn = $('#chat-context-ring-btn') as HTMLButtonElement | null;
  const pctEl = document.getElementById('chat-context-ring-pct');
  const progress = btn?.querySelector<SVGCircleElement>('.mw-context-ring__progress');
  if (!btn || !pctEl || !progress) return;
  mainRingController?.dispose();
  mainRingController = createContextRingController(
    { btn, pct: pctEl, progress },
    {
      getSessionId: () => state.activeSessionId,
      resolveWindow: resolveSessionModelWindow,
      isActive: () => state.activeTab === 'chat',
    },
  );
  mainRingController.refresh();
}
