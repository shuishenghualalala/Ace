/**
 * Dynamic Workflow 阶段时间线（替代/补充 Mermaid 任务依赖图）。
 *
 * 基于 /api/dynamic-kanban/{sid}/status 返回的 workflow definition
 * 实时展示当前执行到哪一阶段、验证门状态、失败回退阶段。
 */

import { backendApi, type ChatChunk, type DynamicKanbanStatus, type WorkflowPhase } from '../backend-client';
import { $, escapeHtml, isDynamicKanbanSession, notify, removeSuppressedSession, state } from '../state';
import { applyChunk, renderChat, stopGeneration } from './chat-controller';
import { discardEmptyOptimisticAssistant, resumeSessionGeneration } from './session-busy';

const STATUS_ICONS: Record<string, string> = {
  done: '✅',
  running: '🔄',
  pending: '⏸',
  paused: '⏸',
  failed: '🚧',
  blocked: '⛔',
};

const STATUS_LABELS: Record<string, string> = {
  done: '已完成',
  running: '进行中',
  pending: '未开始',
  paused: '已暂停',
  failed: '验证未通过',
  blocked: '阻塞',
};

/** workflow 整体状态 → 展示文案。 */
const WORKFLOW_STATUS_LABELS: Record<string, string> = {
  active: '进行中',
  running: '进行中',
  paused: '已暂停',
  done: '已完成',
  failed: '已失败',
};

/** 仅刷新 timeline DOM（不重绘 Mermaid）。 */
export function refreshWorkflowTimelineDom(status: DynamicKanbanStatus | null): void {
  const container = $('#workflow-timeline-container');
  if (!container) return;
  container.innerHTML = buildWorkflowTimelineHtml(status);
  activateWorkflowTimeline(status);
}

function resolvePhaseStatus(
  phase: WorkflowPhase,
  runtime: DynamicKanbanStatus['runtime_state'],
  workflowStatus = '',
): string {
  if (!runtime) return 'pending';
  const completed = runtime.completed_phase_ids || [];
  const results = runtime.phase_results || {};
  const result = results[phase.id];
  if (result && (result.status === 'failed' || result.status === 'blocked')) return 'failed';
  if (completed.includes(phase.id)) return 'done';
  if (runtime.current_phase_id === phase.id) {
    // workflow 已暂停时当前阶段展示为暂停，而不是一直“进行中”。
    return workflowStatus === 'paused' ? 'paused' : 'running';
  }
  return 'pending';
}

function renderPhaseCallResults(phase: WorkflowPhase, runtime: DynamicKanbanStatus['runtime_state']): string {
  const calls = phase.agent_calls || [];
  if (calls.length === 0) return '';
  const pr = runtime?.phase_results?.[phase.id];
  const callResults = pr?.call_results || {};
  return `
    <div class="workflow-timeline__calls">
      ${calls.map((c) => {
        const role = escapeHtml((c as { role?: string }).role || 'agent');
        const callId = String((c as { id?: string }).id || '');
        const result = callId ? callResults[callId] : undefined;
        const status = result?.status || 'pending';
        const icon = STATUS_ICONS[status] || STATUS_ICONS.pending;
        const summary = result?.error || result?.text || '';
        return `
          <div class="workflow-timeline__call workflow-timeline__call--${status}">
            <span class="workflow-timeline__call-icon">${icon}</span>
            <span class="workflow-timeline__call-role">${role}</span>
            ${summary ? `<span class="workflow-timeline__call-summary" title="${escapeHtml(summary)}">${escapeHtml(summary.slice(0, 60))}${summary.length > 60 ? '…' : ''}</span>` : ''}
          </div>
        `;
      }).join('')}
    </div>
  `;
}

function renderPhase(
  phase: WorkflowPhase,
  runtime: DynamicKanbanStatus['runtime_state'],
  workflowStatus = '',
): string {
  const status = resolvePhaseStatus(phase, runtime, workflowStatus);
  const icon = STATUS_ICONS[status] || STATUS_ICONS.pending;
  const label = STATUS_LABELS[status] || STATUS_LABELS.pending;
  return `
    <li class="workflow-timeline__phase workflow-timeline__phase--${status}" data-phase-id="${escapeHtml(phase.id)}">
      <div class="workflow-timeline__indicator">${icon}</div>
      <div class="workflow-timeline__body">
        <div class="workflow-timeline__name">${escapeHtml(phase.name)}</div>
        <div class="workflow-timeline__meta">
          <span class="workflow-timeline__status">${label}</span>
          ${phase.description ? `<span class="workflow-timeline__desc">${escapeHtml(phase.description)}</span>` : ''}
        </div>
        ${renderPhaseCallResults(phase, runtime)}
      </div>
    </li>
  `;
}

export function buildWorkflowTimelineHtml(status: DynamicKanbanStatus | null): string {
  const phases = status?.workflow_definition?.phases || [];
  const runtime = status?.runtime_state || null;
  const workflowStatus = status?.workflow?.status || 'unknown';

  const wfStatusLabel = WORKFLOW_STATUS_LABELS[workflowStatus] || workflowStatus;
  const controls = `
    <div class="workflow-timeline__controls">
      <span class="workflow-timeline__wf-status workflow-timeline__wf-status--${escapeHtml(workflowStatus)}">${escapeHtml(wfStatusLabel)}</span>
      <button type="button" class="workflow-timeline__btn" id="workflow-timeline-refresh" title="刷新状态">⟳ 刷新</button>
      <button type="button" class="workflow-timeline__btn" id="workflow-timeline-pause" ${workflowStatus !== 'active' ? 'disabled' : ''}>⏸ 暂停</button>
      <button type="button" class="workflow-timeline__btn" id="workflow-timeline-resume" ${workflowStatus !== 'paused' ? 'disabled' : ''}>▶️ 继续</button>
      <button type="button" class="workflow-timeline__btn workflow-timeline__btn--danger" id="workflow-timeline-stop">⏹ 停止</button>
    </div>
  `;

  if (phases.length === 0) {
    return `
      <div class="workflow-timeline">
        ${controls}
        <div class="workflow-timeline__empty">当前工作流暂无阶段信息。</div>
      </div>
    `;
  }

  return `
    <div class="workflow-timeline">
      ${controls}
      <ol class="workflow-timeline__list">
        ${phases.map((p) => renderPhase(p, runtime, workflowStatus)).join('')}
      </ol>
    </div>
  `;
}

async function handlePause(): Promise<void> {
  const sid = state.activeSessionId;
  if (!sid || !isDynamicKanbanSession(sid)) return;
  try {
    await backendApi.dynamicKanbanPause(sid);
    notify('已暂停 workflow');
    const status = await backendApi.dynamicKanbanStatus(sid).catch(() => null);
    refreshWorkflowTimelineDom(status);
  } catch (err) {
    notify(`暂停失败：${err instanceof Error ? err.message : String(err)}`);
  }
}

async function handleResume(): Promise<void> {
  const sid = state.activeSessionId;
  if (!sid || !isDynamicKanbanSession(sid)) return;
  // resume 是一个新回合：解除「迟到分片屏蔽」并打开回合 gate。
  // 否则上一回合已封口（turnSealed），带 request_id 的 resume 帧会被
  // turn gate 当作旧回合帧全部丢弃，页面上点继续看不到任何反馈。
  removeSuppressedSession(sid);
  resumeSessionGeneration(sid);
  renderChat();
  try {
    for await (const chunk of backendApi.dynamicKanbanResume(sid)) {
      applyChunk(chunk as ChatChunk);
    }
    // resume 流结束后立即刷新看板状态（active/done）
    const status = await backendApi.dynamicKanbanStatus(sid).catch(() => null);
    refreshWorkflowTimelineDom(status);
  } catch (err) {
    discardEmptyOptimisticAssistant(sid);
    renderChat();
    notify(`继续失败：${err instanceof Error ? err.message : String(err)}`);
    const status = await backendApi.dynamicKanbanStatus(sid).catch(() => null);
    refreshWorkflowTimelineDom(status);
  }
}

function handleStop(): void {
  stopGeneration();
}

export function activateWorkflowTimeline(_status: DynamicKanbanStatus | null): void {
  $('#workflow-timeline-refresh')?.addEventListener('click', async () => {
    const sid = state.activeSessionId;
    if (!sid || !isDynamicKanbanSession(sid)) return;
    const status = await backendApi.dynamicKanbanStatus(sid).catch(() => null);
    refreshWorkflowTimelineDom(status);
  });
  $('#workflow-timeline-pause')?.addEventListener('click', () => void handlePause());
  $('#workflow-timeline-resume')?.addEventListener('click', () => void handleResume());
  $('#workflow-timeline-stop')?.addEventListener('click', () => handleStop());
}
