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
  if (!busy) {
    slot.hidden = true;
    slot.replaceChildren();
    if (runningIntroTimer != null) {
      window.clearInterval(runningIntroTimer);
      runningIntroTimer = null;
    }
    return;
  }

  slot.hidden = false;
  slot.replaceChildren(renderRunningIntro(currentRunningStatus, currentRunningIntro));
  if (runningIntroTimer == null) {
    runningIntroTimer = window.setInterval(advanceRunningIntro, 5000);
  }
}
