/**
 * 执行中轮播文案（composer 上方 #chat-running-intro 槽位）。
 * 文案来源：/api/scenarios/intro-lines 与 loading-status，失败时用内置兜底。
 */

import { backendApi } from '../backend-client';
import { renderRunningIntro } from '../chat-render';
import { $, isBusySession, state } from '../state';

const FALLBACK_RUNNING_INTROS = [
  'Crew 可以把复杂需求拆成可跟踪的任务。',
  'Crew 支持单 Agent 和 Team 两种执行方式。',
  'Crew 会展示工具调用过程，方便你回看关键步骤。',
];
const FALLBACK_RUNNING_STATUSES = [
  '正在为您加速处理中......',
  '全力冲刺中......',
  '正在梳理关键步骤......',
];

let runningIntros = FALLBACK_RUNNING_INTROS;
let runningStatuses = FALLBACK_RUNNING_STATUSES;
let currentRunningIntro = pickNextRunningLine(runningIntros, '');
let currentRunningStatus = pickNextRunningLine(runningStatuses, '');
let runningIntroTimer: number | null = null;
const compactingSessions = new Set<string>();
const compactionStartedAt = new Map<string, number>();
const compactionHideTimers = new Map<string, number>();

export function setContextCompactionActive(sessionId: string, active: boolean): void {
  const pendingTimer = compactionHideTimers.get(sessionId);
  if (pendingTimer != null) {
    window.clearTimeout(pendingTimer);
    compactionHideTimers.delete(sessionId);
  }
  if (active) {
    compactingSessions.add(sessionId);
    compactionStartedAt.set(sessionId, Date.now());
    syncRunningIntroSlot();
    return;
  }
  const elapsed = Date.now() - (compactionStartedAt.get(sessionId) ?? 0);
  const hide = () => {
    compactingSessions.delete(sessionId);
    compactionStartedAt.delete(sessionId);
    compactionHideTimers.delete(sessionId);
    syncRunningIntroSlot();
  };
  const remaining = Math.max(0, 800 - elapsed);
  if (remaining > 0) compactionHideTimers.set(sessionId, window.setTimeout(hide, remaining));
  else hide();
}

function renderCompactionNotice(): HTMLElement {
  const notice = document.createElement('div');
  notice.className = 'context-compaction-notice';
  notice.setAttribute('role', 'status');
  notice.setAttribute('aria-live', 'polite');
  notice.innerHTML = `
    <svg class="nav-agent-logo context-compaction-notice__logo" width="18" height="18" viewBox="3 3 18 18" aria-hidden="true">
      <path class="nav-agent-logo__blob" d="M5.2 13.2c0-4.5 2.9-6.9 6.8-6.9 4.5 0 7 2.8 7 6.2 0 3.8-2.5 5.5-7.2 5.5-4.3 0-6.6-1.4-6.6-4.8Z"></path>
      <path class="nav-agent-logo__cap" d="M9 6.7c.7-1.1 1.7-1.7 3.1-1.7 1.3 0 2.3.5 3 1.5"></path>
      <path class="nav-agent-logo__shine" d="M9.6 10.8v1.9M14.4 10.8v1.9"></path>
      <path class="nav-agent-logo__pixel" d="M18.8 8.2h1.5M19.55 7.45v1.5"></path>
    </svg>
    <strong>正在压缩上下文……</strong>
    <span>如果多次压缩上下文，Agent 的能力会受到影响，建议开启新对话～</span>`;
  return notice;
}

function pickNextRunningLine(lines: string[], current: string): string {
  if (lines.length === 0) return '';
  if (lines.length === 1) return lines[0];
  let next = current;
  while (next === current) {
    next = lines[Math.floor(Math.random() * lines.length)];
  }
  return next;
}

/** 从后端拉取轮播文案（init 时调用一次）。 */
export function loadRunningIntroCopy(): void {
  Promise.allSettled([
    backendApi.scenarioIntroLines(12),
    backendApi.scenarioLoadingStatuses(12),
  ]).then(([introResult, statusResult]) => {
    if (introResult.status === 'fulfilled') {
      const valid = introResult.value.map((item) => item.trim()).filter(Boolean);
      if (valid.length > 0) {
        runningIntros = valid;
        currentRunningIntro = pickNextRunningLine(runningIntros, currentRunningIntro);
      }
    }
    if (statusResult.status === 'fulfilled') {
      const valid = statusResult.value.map((item) => item.trim()).filter(Boolean);
      if (valid.length > 0) {
        runningStatuses = valid;
        currentRunningStatus = pickNextRunningLine(runningStatuses, currentRunningStatus);
      }
    }
    syncRunningIntroSlot();
  }).catch(() => {
    // 单独接口失败不影响任务执行；保留内置兜底文案。
  });
}

function advanceRunningIntro(): void {
  currentRunningIntro = pickNextRunningLine(runningIntros, currentRunningIntro);
  currentRunningStatus = pickNextRunningLine(runningStatuses, currentRunningStatus);
  syncRunningIntroSlot();
}

/** 根据当前 session busy 状态刷新 #chat-running-intro 槽位。 */
export function syncRunningIntroSlot(): void {
  const slot = $('#chat-running-intro') as HTMLElement | null;
  if (!slot) return;
  const sessionId = state.activeSessionId;
  const busy = sessionId ? isBusySession(sessionId) : false;
  if (sessionId && compactingSessions.has(sessionId)) {
    slot.hidden = false;
    if (slot.dataset.renderKey !== 'compaction' || !slot.firstElementChild) {
      slot.replaceChildren(renderCompactionNotice());
      slot.dataset.renderKey = 'compaction';
    }
    return;
  }
  if (!busy) {
    slot.hidden = true;
    slot.replaceChildren();
    delete slot.dataset.renderKey;
    if (runningIntroTimer != null) {
      window.clearInterval(runningIntroTimer);
      runningIntroTimer = null;
    }
    return;
  }

  slot.hidden = false;
  const renderKey = `running\u001f${currentRunningStatus}\u001f${currentRunningIntro}`;
  if (slot.dataset.renderKey !== renderKey || !slot.firstElementChild) {
    slot.replaceChildren(renderRunningIntro(currentRunningStatus, currentRunningIntro));
    slot.dataset.renderKey = renderKey;
  }
  if (runningIntroTimer == null) {
    runningIntroTimer = window.setInterval(advanceRunningIntro, 5000);
  }
}
