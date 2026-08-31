/**
 * Dynamic Kanban 右侧 Inspector「任务」Tab。
 *
 * - 顶部：workflow 阶段时间线 + 控制按钮（暂停/继续/停止/刷新/打开工作目录）
 * - 中部：当前/最近 phase 的 agent_call 执行明细
 * - 底部：后台任务列表（可选折叠）
 */

import { backendApi, type DynamicKanbanStatus, type Task } from '../backend-client';
import { taskStatusLabel } from '../../shared/task-status';
import { $, escapeHtml, isDynamicKanbanSession, notify, saveToStorage, state } from '../state';
import { clearRuntimeStyle, setRuntimeStyle, setRuntimeToken } from '../components/runtime-style';
import {
  activateWorkflowTimeline,
  buildWorkflowTimelineHtml,
  refreshWorkflowTimelineDom,
} from './workflow-timeline';

interface KanbanTask {
  id: string;
  title: string;
  detail?: string;
  assignee?: string | null;
  status: string;
  result_summary?: string;
}

interface KanbanDependency {
  parent_task_id: string;
  child_task_id: string;
}

interface KanbanBoard {
  workflow?: Record<string, unknown>;
  tasks: KanbanTask[];
  dependencies: KanbanDependency[];
  events: unknown[];
}

const STATUS_EMOJI: Record<string, string> = {
  pending: '⏸',
  ready: '⏳',
  running: '🔄',
  done: '✅',
  failed: '❌',
  blocked: '⛔',
};

const STATUS_CLASSES: Record<string, string> = {
  pending: 'status-pending',
  ready: 'status-ready',
  running: 'status-running',
  failed: 'status-failed',
  blocked: 'status-blocked',
  done: 'status-done',
};

const MIN_PANEL_WIDTH = 280;
const MAX_PANEL_WIDTH = 800;
const STORAGE_KEY = 'taskBoardWidth';

let resizeBound = false;
let kanbanRefreshTimer: number | null = null;

// 最新的 /status 快照，用于 workflow timeline 渲染
let latestStatus: DynamicKanbanStatus | null = null;
let kanbanStatusPollingTimer: number | null = null;
let kanbanStatusPollingSessionId: string | null = null;
const KANBAN_STATUS_POLL_MS = 2000;

export async function refreshKanbanBoard(sessionId?: string | null): Promise<void> {
  const sid = sessionId ?? state.activeSessionId;
  // 只有当前会话是专家团队会话时才拉取 Dynamic Kanban board；
  // 普通 agent 会话直接清空看板，避免 404 或无意义请求。
  if (!sid || !isDynamicKanbanSession(sid)) {
    state.kanbanBoard = { tasks: [], dependencies: [], events: [] };
    latestStatus = null;
    state.tasks = [];
    stopKanbanStatusPolling();
    renderKanbanBoard();
    return;
  }
  const [board, status, tasks] = await Promise.all([
    backendApi.dynamicKanbanBoard(sid).catch(() => ({ tasks: [], dependencies: [], events: [] })),
    backendApi.dynamicKanbanStatus(sid).catch(() => null),
    backendApi.tasks(sid).catch(() => []),
  ]);
  state.kanbanBoard = board as unknown as KanbanBoard;
  latestStatus = status;
  // 过滤掉整轮对话的 agent_turn 容器任务，避免用户 query / 流式 chunk 摘要混入看板。
  state.tasks = (tasks as Task[]).filter(isRelevantRuntimeTask);
  renderKanbanBoard();
  // workflow 活跃时启动轮询，只刷新 timeline 状态
  if (shouldKeepPollingStatus(latestStatus)) {
    startKanbanStatusPolling(sid);
  }
}

function shouldKeepPollingStatus(status: DynamicKanbanStatus | null): boolean {
  if (!status) return false;
  const wfStatus = typeof status.workflow?.status === 'string' ? status.workflow.status : '';
  return wfStatus === 'active' || wfStatus === 'paused' || wfStatus === 'running';
}

async function refreshKanbanStatusOnly(sessionId: string): Promise<void> {
  if (sessionId !== state.activeSessionId) return;
  if (!isDynamicKanbanSession(sessionId)) return;
  const status = await backendApi.dynamicKanbanStatus(sessionId).catch(() => null);
  // 异步请求期间用户可能已切换会话，返回前再次校验
  if (sessionId !== state.activeSessionId) return;
  latestStatus = status;
  refreshWorkflowTimelineDom(status);
  if (!shouldKeepPollingStatus(status)) {
    stopKanbanStatusPolling();
  }
}

function startKanbanStatusPolling(sessionId: string): void {
  if (kanbanStatusPollingTimer !== null) {
    // 同一会话已在轮询，避免重复启动
    if (kanbanStatusPollingSessionId === sessionId) return;
    // 会话已切换，停止旧轮询并启动新轮询
    stopKanbanStatusPolling();
  }
  kanbanStatusPollingSessionId = sessionId;
  kanbanStatusPollingTimer = window.setInterval(() => {
    void refreshKanbanStatusOnly(sessionId);
  }, KANBAN_STATUS_POLL_MS);
}

export function stopKanbanStatusPolling(): void {
  if (kanbanStatusPollingTimer !== null) {
    window.clearInterval(kanbanStatusPollingTimer);
    kanbanStatusPollingTimer = null;
  }
  kanbanStatusPollingSessionId = null;
}

/**
 * 流式过程中频繁刷新看板会大量请求后端，这里做 300ms 节流：
 * 同一时间段内的多次 chunk 只触发一次后端拉取。
 */
export function scheduleRefreshKanbanBoard(sessionId?: string | null): void {
  if (kanbanRefreshTimer !== null) return;
  kanbanRefreshTimer = window.setTimeout(() => {
    kanbanRefreshTimer = null;
    void refreshKanbanBoard(sessionId);
  }, 300);
}

export function isRelevantRuntimeTask(task: Task): boolean {
  // agent_turn 是 dispatcher 为整轮对话创建的容器任务，title 为用户 query，
  // progress 里会记录流式 chunk 摘要，不应在看板里展示。
  if (task.kind === 'agent_turn') return false;
  // 只看板展示真正后台化或子任务性质的任务。
  if (task.backgrounded) return true;
  if (task.kind && ['shell', 'subagent', 'team'].includes(task.kind)) return true;
  // 其他前台任务（如旧 Team 模式遗留）也允许展示，但 agent_turn 必须过滤。
  return true;
}

export function bindTaskBoardResize(): void {
  if (resizeBound) return;
  const handle = $('#task-board-resize-handle') as HTMLElement | null;
  if (!handle) return;

  let resizing = false;
  let startX = 0;
  let startWidth = state.taskBoardWidth;

  handle.addEventListener('mousedown', (e) => {
    resizing = true;
    startX = e.clientX;
    startWidth = state.taskBoardWidth;
    setRuntimeStyle(document.body, 'cursor', 'col-resize');
    setRuntimeStyle(document.body, 'userSelect', 'none');
    e.preventDefault();
  });

  window.addEventListener('mousemove', (e) => {
    if (!resizing) return;
    const delta = startX - e.clientX;
    const next = Math.min(MAX_PANEL_WIDTH, Math.max(MIN_PANEL_WIDTH, startWidth + delta));
    state.taskBoardWidth = next;
    applyTaskBoardWidth(next);
  });

  window.addEventListener('mouseup', () => {
    if (!resizing) return;
    resizing = false;
    clearRuntimeStyle(document.body, 'cursor');
    clearRuntimeStyle(document.body, 'userSelect');
    saveToStorage(STORAGE_KEY, state.taskBoardWidth);
  });

  resizeBound = true;
}

function applyTaskBoardWidth(width: number): void {
  const panel = $('#task-board-panel') as HTMLElement | null;
  const handle = $('#task-board-resize-handle') as HTMLElement | null;
  if (panel) setRuntimeStyle(panel, 'width', `${width}px`);
  if (handle) setRuntimeStyle(handle, 'right', `${width}px`);
  setRuntimeToken(document.documentElement, '--mw-task-board-width', `${width}px`);
}

function hideLegacyTaskBoardPanel(): void {
  const panel = $('#task-board-panel');
  const handle = $('#task-board-resize-handle') as HTMLElement | null;
  if (panel) panel.hidden = true;
  if (handle) handle.hidden = true;
}

/** 断开看板相关的副作用（切换离开任务 Tab 时调用）。 */
export function disconnectKanbanObserver(): void {
  stopKanbanStatusPolling();
}

/** 构建 Inspector「任务」Tab 的静态 HTML 骨架。 */
export function buildKanbanInspectorHtml(): string {
  const isKanbanMode = isDynamicKanbanSession(state.activeSessionId);
  const board = (state.kanbanBoard || { tasks: [], dependencies: [], events: [] }) as KanbanBoard;
  const tasks = board.tasks;
  const counts = countByStatus(tasks);
  const runtimeListHtml = runtimeTasksHtml(state.tasks);

  if (!isKanbanMode) {
    return `<div class="inspector-empty">当前会话未绑定专家团队，任务看板仅在专家团（Dynamic Kanban）模式下可用。</div>`;
  }

  const timelineHtml = buildWorkflowTimelineHtml(latestStatus);

  return `
    <div class="inspector-kanban">
      <div class="board kanban-board">
        <div class="kanban-board__header">
          <div class="kanban-board__summary">
            <span class="kanban-board__badge done">已完成 ${counts.done || 0}</span>
            <span class="kanban-board__badge running">进行中 ${counts.running || 0}</span>
            <span class="kanban-board__badge ready">待执行 ${counts.ready || 0}</span>
            <span class="kanban-board__badge failed">失败 ${counts.failed || 0}</span>
            <span class="kanban-board__badge blocked">阻塞 ${counts.blocked || 0}</span>
          </div>
        </div>
        <div class="kanban-board__scrollable">
          <div id="workflow-timeline-container" class="workflow-timeline-container">
            ${timelineHtml}
          </div>
          ${buildExecutionDetailHtml(latestStatus)}
          ${runtimeListHtml ? `<details class="task-runtime__details"><summary>后台任务</summary><div class="task-runtime__list">${runtimeListHtml}</div></details>` : ''}
        </div>
      </div>
    </div>
  `;
}

function countByStatus(tasks: KanbanTask[]): Record<string, number> {
  const counts: Record<string, number> = {};
  tasks.forEach((t) => {
    counts[t.status] = (counts[t.status] || 0) + 1;
  });
  return counts;
}

/** 构建当前/最近 phase 的 agent_call 执行明细 HTML。 */
function buildExecutionDetailHtml(status: DynamicKanbanStatus | null): string {
  if (!status) return '';
  const runtime = status.runtime_state;
  if (!runtime) return '';
  const phases = status.workflow_definition?.phases || [];
  if (phases.length === 0) return '';

  const items: string[] = [];
  for (const phase of phases) {
    const pr = runtime.phase_results?.[phase.id];
    if (!pr) continue;
    const calls = pr.call_results || {};
    const callIds = Object.keys(calls);
    if (callIds.length === 0) continue;

    const callHtml = callIds.map((callId) => {
      const call = calls[callId] as { status?: string; text?: string; error?: string; artifacts?: string[]; role?: string } | undefined;
      if (!call) return '';
      const s = call.status || 'running';
      const icon = STATUS_EMOJI[s] || STATUS_EMOJI.pending;
      const cls = STATUS_CLASSES[s] || STATUS_CLASSES.pending;
      const summary = call.error || call.text || '执行中…';
      const artifacts = (call.artifacts || []).map((a) => renderFilePathActions(String(a), 'kanban-board__artifact')).join('');
      return `
        <li class="kanban-board__call kanban-board__call--${cls}">
          <span class="kanban-board__call-icon">${icon}</span>
          <div class="kanban-board__call-body">
            <div class="kanban-board__call-role">${escapeHtml(call.role || callId)}</div>
            <div class="kanban-board__call-summary">${escapeHtml(summary.slice(0, 240))}${summary.length > 240 ? '…' : ''}</div>
            ${artifacts ? `<div class="kanban-board__call-artifacts">${artifacts}</div>` : ''}
          </div>
        </li>
      `;
    }).join('');

    items.push(`
      <div class="kanban-board__phase-detail" data-phase-id="${escapeHtml(phase.id)}">
        <div class="kanban-board__phase-name">${escapeHtml(phase.name)}</div>
        <ul class="kanban-board__call-list">${callHtml}</ul>
      </div>
    `);
  }

  if (items.length === 0) return '';

  return `
    <details class="kanban-board__execution-details" open>
      <summary>执行明细</summary>
      <div class="kanban-board__execution-list">
        ${items.join('')}
      </div>
    </details>
  `;
}

/** 绑定 Inspector「任务」Tab 的交互。 */
export function activateKanbanInspectorTab(): void {
  if (!isDynamicKanbanSession(state.activeSessionId)) return;

  document.querySelectorAll<HTMLButtonElement>('.task-runtime__cancel').forEach((button) => {
    button.addEventListener('click', () => {
      const taskId = button.dataset.taskId;
      if (!taskId) return;
      button.disabled = true;
      void backendApi.cancelTask(taskId).then(() => refreshKanbanBoard()).catch(() => {
        button.disabled = false;
      });
    });
  });

  document.querySelectorAll<HTMLButtonElement>('[data-kanban-open-path]').forEach((button) => {
    button.addEventListener('click', (event) => {
      event.preventDefault();
      event.stopPropagation();
      const targetPath = button.dataset.kanbanOpenPath;
      if (targetPath) void openKanbanPath(targetPath);
    });
  });

  activateWorkflowTimeline(latestStatus);
}

function isAbsolutePath(value: string): boolean {
  return /^[a-zA-Z]:[\\/]/.test(value) || value.startsWith('/') || value.startsWith('\\\\');
}

function containingFolderPath(value: string): string {
  const index = Math.max(value.lastIndexOf('\\'), value.lastIndexOf('/'));
  if (index < 0) return '';
  if (index === 2 && value[1] === ':') return value.slice(0, 3);
  if (index === 0) return value.slice(0, 1);
  return value.slice(0, index);
}

function renderFilePathActions(rawPath: string, codeClass: string): string {
  const filePath = rawPath.trim();
  const escaped = escapeHtml(filePath);
  const folderPath = containingFolderPath(filePath);
  const actions = isAbsolutePath(filePath)
    ? `
      <span class="kanban-board__path-actions">
        <button type="button" class="kanban-board__path-btn" data-kanban-open-path="${escaped}" title="打开文件">打开</button>
        ${folderPath ? `<button type="button" class="kanban-board__path-btn" data-kanban-open-path="${escapeHtml(folderPath)}" title="打开所属文件夹">文件夹</button>` : ''}
      </span>
    `
    : '';
  return `<span class="kanban-board__path"><code class="${codeClass}" title="${escaped}">${escaped}</code>${actions}</span>`;
}

async function openKanbanPath(targetPath: string): Promise<void> {
  if (!window.Crew?.openPath) {
    notify('当前环境不支持打开文件');
    return;
  }
  try {
    const workspaceRoot = resolveKanbanWorkspaceRootPath();
    const result = await window.Crew.openPath(targetPath, workspaceRoot);
    if (result) notify(`打开失败：${result}`);
  } catch (err) {
    notify(`打开失败：${err instanceof Error ? err.message : String(err)}`);
  }
}

/**
 * 归一化 openPath 的 allowedRoot：后端在无项目工作空间时会把
 * workspace_root_path 存成 ""，直接透传会触发 IPC_ARG_VALIDATION_FAILED，
 * 空串 / 空白串 / 非字符串统一归一化为 undefined。
 */
export function normalizeAllowedRoot(value: unknown): string | undefined {
  const root = typeof value === 'string' ? value.trim() : '';
  return root || undefined;
}

/** 从当前看板数据中提取关联的项目工作空间根目录。 */
function resolveKanbanWorkspaceRootPath(): string | undefined {
  const board = (state.kanbanBoard || { workflow: undefined }) as {
    workflow?: { context?: Record<string, unknown> };
  };
  return normalizeAllowedRoot(board.workflow?.context?.workspace_root_path);
}

/** 看板数据更新后，若 Inspector 任务 Tab 已挂载则刷新 DOM。 */
export function refreshKanbanInspectorIfMounted(): void {
  if (!$('#workflow-timeline-container')) return;
  const body = $('#chat-inspector-body');
  if (!body) return;
  // 保留滚动位置： Inspector body 本身和内部看板滚动区都可能是实际滚动容器。
  const savedBodyScrollTop = body.scrollTop;
  const scrollable = body.querySelector('.kanban-board__scrollable') as HTMLElement | null;
  const savedScrollTop = scrollable ? scrollable.scrollTop : 0;
  body.innerHTML = buildKanbanInspectorHtml();
  activateKanbanInspectorTab();
  body.scrollTop = savedBodyScrollTop;
  const newScrollable = body.querySelector('.kanban-board__scrollable') as HTMLElement | null;
  if (newScrollable) {
    newScrollable.scrollTop = savedScrollTop;
  }
}

function runtimeTasksHtml(tasks: Task[]): string {
  const relevant = tasks.filter(isRelevantRuntimeTask);
  if (relevant.length === 0) return '';
  return relevant.map((task) => {
    const id = task.task_id || task.id;
    const canCancel = task.status === 'pending' || task.status === 'running';
    const flags = [
      task.kind || 'team',
      task.backgrounded ? (task.auto_backgrounded ? '自动后台' : '后台') : '前台',
      task.last_activity_at ? `活动 ${new Date(task.last_activity_at * 1000).toLocaleTimeString()}` : '',
    ].filter(Boolean).join(' · ');
    const progress = task.progress && Object.keys(task.progress).length
      ? `<pre class="task-runtime__progress">${escapeHtml(JSON.stringify(task.progress, null, 2))}</pre>`
      : '';
    const outcome = task.error || task.result;
    return `
      <article class="task-runtime__item">
        <div class="task-runtime__title">${escapeHtml(task.title || id)}</div>
        <div class="task-runtime__meta">${escapeHtml(taskStatusLabel(task.status))} · ${escapeHtml(flags)}</div>
        ${progress}
        ${outcome ? `<div class="task-runtime__result">${escapeHtml(outcome)}</div>` : ''}
        ${task.output_ref ? `<div class="task-runtime__output">输出：${renderFilePathActions(task.output_ref, 'task-runtime__output-path')}</div>` : ''}
        ${canCancel ? `<button type="button" class="task-runtime__cancel" data-task-id="${escapeHtml(id)}">取消</button>` : ''}
      </article>
    `;
  }).join('');
}

export function renderKanbanBoard(): void {
  hideLegacyTaskBoardPanel();
  refreshKanbanInspectorIfMounted();
}
