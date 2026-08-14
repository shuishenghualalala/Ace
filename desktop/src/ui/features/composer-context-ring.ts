/**
 * Composer 上下文占比圆环：模型与发送按钮之间，实时展示当前会话 token 占用。
 */

import { backendApi } from '../backend-client';
import { $, state } from '../state';
import { resolveSessionModelWindow } from './session-model';

const RING_RADIUS = 9;
const RING_CIRC = 2 * Math.PI * RING_RADIUS;

let lastSessionId: string | null = null;
let inflight: Promise<void> | null = null;

/** 将 token 数格式化为 K/M 缩写（与 WorkBuddy 提示风格接近）。 */
function formatTokenCount(n: number): string {
  const v = Math.max(0, Math.round(n));
  if (v >= 1_000_000) return `${(v / 1_000_000).toFixed(1)}M`;
  if (v >= 1000) return `${(v / 1000).toFixed(1)}K`;
  return String(v);
}

function setRingProgress(ratio: number): void {
  const progress = document.querySelector<SVGCircleElement>(
    '#chat-context-ring-btn .mw-context-ring__progress',
  );
  const btn = $('#chat-context-ring-btn');
  const pctEl = document.getElementById('chat-context-ring-pct');
  if (!progress) return;
  const clamped = Math.max(0, Math.min(1, ratio));
  progress.setAttribute('stroke-dasharray', String(RING_CIRC));
  progress.setAttribute('stroke-dashoffset', String(RING_CIRC * (1 - clamped)));
  const pct = Math.round(clamped * 100);
  if (pctEl) pctEl.textContent = pct >= 100 ? '满' : `${pct}%`;
  if (btn) {
    btn.classList.remove('is-warn', 'is-critical');
    if (clamped >= 0.9) btn.classList.add('is-critical');
    else if (clamped >= 0.7) btn.classList.add('is-warn');
  }
}

function setRingTooltip(used: number, max: number, ratio: number): void {
  const btn = $('#chat-context-ring-btn');
  if (!btn) return;
  const pct = (ratio * 100).toFixed(1);
  btn.title = `${pct}% · ${formatTokenCount(used)} / ${formatTokenCount(max)} 上下文已使用`;
  btn.setAttribute('aria-label', btn.title);
}

function hideRing(): void {
  const btn = $('#chat-context-ring-btn');
  if (!btn) return;
  btn.hidden = true;
  setRingProgress(0);
  btn.classList.remove('is-warn', 'is-critical');
  btn.title = '上下文占用';
  const pctEl = document.getElementById('chat-context-ring-pct');
  if (pctEl) pctEl.textContent = '0%';
}

/** 拉取并刷新当前会话上下文圆环。 */
export function refreshComposerContextRing(): void {
  const sid = state.activeSessionId;
  const btn = $('#chat-context-ring-btn');
  if (!btn) return;

  if (!sid) {
    hideRing();
    lastSessionId = null;
    return;
  }

  btn.hidden = false;
  if (sid === lastSessionId && inflight) return;

  const run = async (): Promise<void> => {
    try {
      const ctx = await backendApi.sessionContext(sid);
      if (state.activeSessionId !== sid) return;
      lastSessionId = sid;
      // 分母用会话绑定模型的窗口；网关返回的 max_tokens 是全局窗口，直接用会与 Inspector 口径不一致。
      const max = resolveSessionModelWindow();
      const ratio = max > 0 ? ctx.used_tokens / max : 0;
      setRingProgress(ratio);
      setRingTooltip(ctx.used_tokens, max, ratio);
    } catch {
      if (state.activeSessionId !== sid) return;
      setRingProgress(0);
      btn.title = '上下文占用（暂无法获取）';
    }
  };

  inflight = run().finally(() => {
    inflight = null;
  });
}

export function bindComposerContextRing(): void {
  refreshComposerContextRing();
  window.addEventListener('session:changed', () => {
    lastSessionId = null;
    refreshComposerContextRing();
  });
  window.addEventListener('messages:changed', () => refreshComposerContextRing());
  window.addEventListener('session:model-changed', () => {
    lastSessionId = null;
    refreshComposerContextRing();
  });
}
