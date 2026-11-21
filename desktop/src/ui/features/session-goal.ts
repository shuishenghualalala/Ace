import { backendApi } from '../backend-client';
import type { SessionGoalAction, SessionGoalRunState, SessionGoalSnapshot } from '../api/feature-session';
import { notify, state } from '../state';
import { authStore, productModeStore, sessionStore, uiStore } from '../stores/stores';
import { logStream } from '../stream-debug';
import { isRendererLoggedIn } from './auth-gate';
import { isChannelSessionId } from './channel-sessions';
import { queryPrimaryComposer } from './composer-scope';
import { interactiveOwnerKey } from './interactive-owner';
import { isStudioView } from './studio-chrome-state';

const STATUS = { active: '已启用', paused: '已暂停', completed: '已完成' };
const RUN_STATUS: Record<SessionGoalRunState, string> = {
  accepted: '本轮已接收',
  running: '正在推进本轮',
  stopping: '正在停止本轮',
  finished: '本轮已结束',
  stopped: '本轮已停止',
  interrupted: '本轮已中断',
  failed: '本轮推进失败',
};
const RUN_REASONS: Record<string, string> = {
  paused_before_dispatch: '开始前已暂停。',
  pause_requested: '已请求暂停，等待本轮停止。',
  gateway_restarted: '网关已重启，本轮不会重放。',
  gateway_shutdown: '网关正在关闭。',
  goal_deadline: '已达到本轮推进时限。',
  goal_session_busy: '会话有其他工作正在进行。',
  goal_gateway_busy: '网关当前没有可用的执行槽。',
  goal_gateway_draining: '网关正在收尾。',
  goal_plan_active: '会话正在计划模式中。',
  goal_runtime_unavailable: '目标任务运行时未就绪。',
  goal_execution_changed: '推进的执行条件已变化。',
  goal_step_superseded: '目标已暂停或会话已变化。',
  goal_workspace_changed: '会话工作空间已变化。',
  goal_owner_changed: '账号状态已变化。',
  goal_identity_lost: '无法继续原有的目标执行。',
  goal_worker_cancelled: '目标任务已取消。',
  goal_dispatch_failed: '无法调度目标任务。',
  goal_no_terminal: '网关未确认本轮执行结果。',
};
const isLive = (run: SessionGoalSnapshot['run']) =>
  run?.state === 'accepted' || run?.state === 'running' || run?.state === 'stopping';
const OBSERVATION_INTERVAL_MS = 2000;
const MAX_OBSERVATION_READS = 75;
const MAX_OBJECTIVE = 4000;
let disposeGoal: (() => void) | undefined;

export function closeDesktopSessionGoal(): void {
  disposeGoal?.();
}

/** Manage the saved goal without dispatching a conversation turn. */
export function openDesktopSessionGoal(objective: string): boolean {
  const sessionId = state.activeSessionId;
  const row = state.sessions.find(({ id }) => id === sessionId);
  const host = document.getElementById('chat-panel');
  if (!isRendererLoggedIn()) return false;
  if (!sessionId || !row || !host || host.hidden) {
    notify('请先打开有历史的内置主会话，再设置目标。');
    return false;
  }
  if (state.mode !== 'agent' || row.archived || (row.agentBinding && row.agentBinding.kind !== 'builtin')
    || sessionId.includes('::') || isChannelSessionId(sessionId)) {
    notify('当前会话不支持设置目标。');
    return false;
  }
  closeDesktopSessionGoal();
  const owner = interactiveOwnerKey();
  const generation = state.backendReadiness?.generation;
  const controller = new AbortController();
  const { signal } = controller;
  const isCurrent = () => !signal.aborted && isRendererLoggedIn() && owner === interactiveOwnerKey()
    && state.activeSessionId === sessionId && state.backendReadiness?.generation === generation
    && state.activeTab === 'chat' && !isStudioView() && productModeStore.get().productMode === 'assistant';
  if (!isCurrent()) return false;

  const panel = document.createElement('section');
  panel.className = 'desktop-goal';
  panel.setAttribute('aria-label', '会话目标');
  panel.innerHTML = `
    <header class="desktop-goal__row"><strong data-title>会话目标</strong>
      <button class="btn btn-ghost" type="button" data-close>关闭目标面板</button></header>
    <p class="desktop-goal__hint">活动目标会加入后续聊天的上下文。暂停不会停止普通聊天，保存或恢复不会自动发起回合。</p>
    <p role="status" class="desktop-goal__feedback" data-feedback></p>
    <p role="status" class="desktop-goal__feedback" data-run hidden><span data-run-state></span> <span data-run-reason hidden></span></p>
    <p class="desktop-goal__objective" data-objective hidden></p>
    <form data-editor hidden>
      <textarea aria-label="目标内容" aria-describedby="desktop-goal-validation" rows="2" placeholder="描述想达成的结果"></textarea>
      <p id="desktop-goal-validation" role="status" class="desktop-goal__feedback" data-validation></p>
      <div class="desktop-goal__row">
        <button class="btn btn-primary" type="submit">保存目标</button>
        <button class="btn btn-ghost" type="button" data-cancel-edit>取消编辑</button>
      </div>
    </form>
    <div class="desktop-goal__row" data-actions hidden>
      <button class="btn btn-ghost" type="button" data-advance title="推进已保存的目标，不保存当前草稿">推进一轮</button>
      <button class="btn btn-ghost" type="button" data-pause>暂停目标</button>
      <button class="btn btn-ghost" type="button" data-resume>恢复目标</button>
      <button class="btn btn-ghost" type="button" data-complete>标记目标完成</button>
      <button class="btn btn-ghost" type="button" data-edit>设置新目标</button>
    </div>
    <button class="btn btn-ghost" type="button" data-reload hidden>重新读取目标</button>`;
  const title = panel.querySelector<HTMLElement>('[data-title]')!;
  const close = panel.querySelector<HTMLButtonElement>('[data-close]')!;
  const feedback = panel.querySelector<HTMLElement>('[data-feedback]')!;
  const runStatus = panel.querySelector<HTMLElement>('[data-run]')!;
  const runState = runStatus.querySelector<HTMLElement>('[data-run-state]')!;
  const runReason = runStatus.querySelector<HTMLElement>('[data-run-reason]')!;
  const displayed = panel.querySelector<HTMLElement>('[data-objective]')!;
  const form = panel.querySelector<HTMLFormElement>('[data-editor]')!;
  const input = form.querySelector('textarea')!;
  const validation = form.querySelector('[data-validation]')!;
  const save = form.querySelector<HTMLButtonElement>('[type=submit]')!;
  const cancelEdit = form.querySelector<HTMLButtonElement>('[data-cancel-edit]')!;
  const actions = panel.querySelector<HTMLElement>('[data-actions]')!;
  const advance = actions.querySelector<HTMLButtonElement>('[data-advance]')!;
  const pause = actions.querySelector<HTMLButtonElement>('[data-pause]')!;
  const resume = actions.querySelector<HTMLButtonElement>('[data-resume]')!;
  const complete = actions.querySelector<HTMLButtonElement>('[data-complete]')!;
  const edit = actions.querySelector<HTMLButtonElement>('[data-edit]')!;
  const reload = panel.querySelector<HTMLButtonElement>('[data-reload]')!;
  let snapshot: SessionGoalSnapshot | null = null;
  let editing = Boolean(objective);
  let busy = false;
  let observeEpoch = 0;
  let observeRemaining = MAX_OBSERVATION_READS;
  let observationHalted = false;
  let observeTimer: ReturnType<typeof setTimeout> | undefined;
  let pendingObservation: Promise<void> | undefined;
  input.value = objective;
  const inputLength = () => Array.from(input.value.trim()).length;
  const refreshSave = () => {
    const length = inputLength();
    const tooLong = length > MAX_OBJECTIVE;
    input.setAttribute('aria-invalid', String(tooLong));
    validation.textContent = tooLong ? `目标最多 ${MAX_OBJECTIVE} 个字符` : '';
    save.disabled = busy || !snapshot || isLive(snapshot.run) || length < 1 || tooLong;
  };
  const render = () => {
    const goal = snapshot?.goal;
    const run = snapshot?.run;
    const live = isLive(run);
    title.textContent = goal ? `会话目标 · ${STATUS[goal.status]}` : '会话目标';
    const stepLabel = run ? RUN_STATUS[run.state] : '';
    if (runState.textContent !== stepLabel) runState.textContent = stepLabel;
    runStatus.hidden = !run;
    const reason = run?.state === 'finished' ? '本轮结束不代表目标达成。'
      : run?.code && Object.hasOwn(RUN_REASONS, run.code) ? RUN_REASONS[run.code] : '';
    if (runReason.textContent !== reason) runReason.textContent = reason;
    runReason.hidden = !reason;
    displayed.hidden = !goal;
    displayed.textContent = goal?.objective ?? '';
    form.hidden = !editing && snapshot?.goal !== null;
    input.readOnly = busy;
    cancelEdit.hidden = !goal;
    advance.hidden = goal?.status !== 'active' || live || !(snapshot?.supports_advance === true || run);
    pause.hidden = goal?.status !== 'active' || (!form.hidden && !live);
    resume.hidden = goal?.status !== 'paused' || !form.hidden;
    complete.hidden = !goal || goal.status === 'completed' || !form.hidden;
    edit.hidden = !goal || !form.hidden;
    actions.hidden = [advance, pause, resume, complete, edit].every((button) => button.hidden);
    for (const button of [cancelEdit, advance, pause, edit]) button.disabled = busy;
    resume.disabled = complete.disabled = busy || live;
    reload.hidden = Boolean(snapshot && !observationHalted && snapshot.supports_advance !== true && !run);
    reload.disabled = busy || Boolean(pendingObservation);
    refreshSave();
  };
  const restoreActionFocus = (previous: Element | null) => {
    const focused = document.activeElement;
    if (!previous || !panel.contains(previous) || (focused !== previous && focused !== document.body)) return;
    const candidates = !snapshot || observationHalted || previous === reload ? [reload]
      : !form.hidden ? [input] : [pause, resume, advance, edit, reload];
    const next = candidates.find((element) => !element.hidden && !element.disabled);
    next?.focus();
  };
  const problem = (error: unknown) => error instanceof Error ? error.message : '网关未返回可确认的结果';
  const report = (action: SessionGoalAction | 'read', operationId: string, started: number, result: string) => {
    logStream('session-goal', 'request-settled', { action, operationId, result, durationMs: performance.now() - started });
  };
  const resetObservation = () => {
    observationHalted = false;
    observeRemaining = MAX_OBSERVATION_READS;
  };
  const invalidateObservation = () => {
    observeEpoch += 1;
    if (observeTimer !== undefined) clearTimeout(observeTimer);
    observeTimer = undefined;
  };
  const load = async (operationId: string, onLoaded = '', observedEpoch?: number) => {
    const automatic = observedEpoch !== undefined;
    const canUse = () => isCurrent() && (!automatic || observedEpoch === observeEpoch);
    if (!automatic && pendingObservation) await pendingObservation;
    if (!canUse()) return;
    const started = performance.now();
    try {
      const latest = await backendApi.sessionGoal(sessionId, signal);
      if (!canUse()) return;
      snapshot = latest;
      if (!automatic) {
        resetObservation();
        feedback.textContent = onLoaded;
      }
      report('read', operationId, started, 'ok');
    } catch (error) {
      if (!canUse()) return;
      observationHalted = true;
      feedback.textContent = automatic ? `无法刷新本轮推进状态：${problem(error)}。请重新读取目标。`
        : `无法读取目标：${problem(error)}`;
      report('read', operationId, started, 'failed');
    }
  };
  const scheduleObservation = () => {
    if (!isCurrent() || busy || observationHalted || !isLive(snapshot?.run)
      || observeTimer !== undefined || pendingObservation) return;
    if (observeRemaining <= 0) {
      observationHalted = true;
      feedback.textContent = '已停止自动刷新，请重新读取目标确认本轮状态。';
      render();
      return;
    }
    observeTimer = setTimeout(() => {
      observeTimer = undefined;
      if (!isCurrent()) return;
      observeRemaining -= 1;
      pendingObservation = load(crypto.randomUUID(), '', observeEpoch).finally(() => {
        pendingObservation = undefined;
        if (isCurrent()) {
          render();
          scheduleObservation();
        }
      });
      render();
    }, OBSERVATION_INTERVAL_MS);
  };
  const read = async () => {
    if (!isCurrent() || busy) return;
    const previous = document.activeElement;
    busy = true;
    invalidateObservation();
    feedback.textContent = '正在读取目标…';
    render();
    await load(crypto.randomUUID());
    if (!isCurrent()) return;
    busy = false;
    render();
    scheduleObservation();
    if (previous === reload) restoreActionFocus(previous);
    else if (!form.hidden && document.activeElement === close) input.focus();
  };
  const apply = async (action: SessionGoalAction) => {
    if (!isCurrent() || busy || !snapshot || (action !== 'pause' && isLive(snapshot.run))) return;
    if ((action === 'set' && save.disabled) || (action === 'advance' && advance.hidden)) return;
    const previous = document.activeElement;
    const operationId = crypto.randomUUID();
    const started = performance.now();
    const data = {
      version: snapshot.version, action, ...(action === 'set' ? { objective: input.value.trim() } : {}),
    };
    busy = true;
    // A GET launched before this explicit write cannot confirm state after it.
    invalidateObservation();
    feedback.textContent = '正在更新目标…';
    render();
    try {
      const result = await backendApi.changeSessionGoal(sessionId, data, signal);
      if (!isCurrent()) return;
      if (result.kind === 'applied') {
        snapshot = result.snapshot;
        resetObservation();
        if (action === 'set') editing = false;
        feedback.textContent = action === 'advance' ? '' : '目标已更新';
        report(action, operationId, started, 'applied');
      } else if (result.kind === 'version-conflict') {
        snapshot = null;
        report(action, operationId, started, 'stale');
        feedback.textContent = '正在读取最新目标…';
        await load(operationId, '目标或会话已更新。请核对当前状态后再提交。');
      } else {
        observationHalted = true;
        feedback.textContent = result.message;
        report(action, operationId, started, 'rejected');
      }
    } catch (error) {
      if (!isCurrent()) return;
      snapshot = null; // A lost reply is not proof that the write failed. Require a read before trying again.
      observationHalted = true;
      feedback.textContent = `未能确认目标更新。请重新读取：${problem(error)}`;
      report(action, operationId, started, 'unconfirmed');
    } finally {
      if (isCurrent()) {
        busy = false;
        render();
        scheduleObservation();
        restoreActionFocus(previous);
      }
    }
  };

  form.addEventListener('submit', (event) => { event.preventDefault(); void apply('set'); });
  input.addEventListener('input', refreshSave);
  close.onclick = () => {
    closeDesktopSessionGoal();
    queryPrimaryComposer('[data-composer-input]')?.focus();
  };
  edit.onclick = () => { input.value = snapshot?.goal?.objective ?? ''; editing = true; render(); input.focus(); };
  cancelEdit.onclick = () => { editing = false; render(); edit.focus(); };
  advance.onclick = () => { void apply('advance'); };
  pause.onclick = () => { void apply('pause'); };
  resume.onclick = () => { void apply('resume'); };
  complete.onclick = () => { void apply('complete'); };
  reload.onclick = () => { void read(); };

  const onScopeChange = () => { if (!signal.aborted && !isCurrent()) closeDesktopSessionGoal(); };
  const unsubscribe = [sessionStore.subscribe(onScopeChange), authStore.subscribe(onScopeChange),
    uiStore.subscribe(onScopeChange), productModeStore.subscribe(onScopeChange)];
  window.addEventListener('studio-chrome:changed', onScopeChange, { signal });
  disposeGoal = () => {
    disposeGoal = undefined;
    invalidateObservation();
    controller.abort();
    for (const unbind of unsubscribe) unbind();
    panel.remove();
  };
  host.prepend(panel);
  void read();
  (objective ? input : close).focus();
  return true;
}
