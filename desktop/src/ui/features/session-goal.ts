import { backendApi } from '../backend-client';
import type { SessionGoalAction, SessionGoalSnapshot } from '../api/feature-session';
import { notify, state } from '../state';
import { authStore, productModeStore, sessionStore, uiStore } from '../stores/stores';
import { logStream } from '../stream-debug';
import { isRendererLoggedIn } from './auth-gate';
import { isChannelSessionId } from './channel-sessions';
import { queryPrimaryComposer } from './composer-scope';
import { interactiveOwnerKey } from './interactive-owner';
import { isStudioView } from './studio-chrome-state';

const STATUS = { active: '已启用', paused: '已暂停', completed: '已完成' };
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
    <p class="desktop-goal__hint">活动目标会加入后续聊天的上下文。暂停不会停止当前回复，保存或恢复不会自动发起回合。</p>
    <p role="status" class="desktop-goal__feedback" data-feedback></p>
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
      <button class="btn btn-ghost" type="button" data-pause>暂停目标</button>
      <button class="btn btn-ghost" type="button" data-resume>恢复目标</button>
      <button class="btn btn-ghost" type="button" data-complete>标记目标完成</button>
      <button class="btn btn-ghost" type="button" data-edit>设置新目标</button>
    </div>
    <button class="btn btn-ghost" type="button" data-reload hidden>重新读取目标</button>`;
  const title = panel.querySelector<HTMLElement>('[data-title]')!;
  const close = panel.querySelector<HTMLButtonElement>('[data-close]')!;
  const feedback = panel.querySelector<HTMLElement>('[data-feedback]')!;
  const displayed = panel.querySelector<HTMLElement>('[data-objective]')!;
  const form = panel.querySelector<HTMLFormElement>('[data-editor]')!;
  const input = form.querySelector('textarea')!;
  const validation = form.querySelector('[data-validation]')!;
  const save = form.querySelector<HTMLButtonElement>('[type=submit]')!;
  const cancelEdit = form.querySelector<HTMLButtonElement>('[data-cancel-edit]')!;
  const actions = panel.querySelector<HTMLElement>('[data-actions]')!;
  const pause = actions.querySelector<HTMLButtonElement>('[data-pause]')!;
  const resume = actions.querySelector<HTMLButtonElement>('[data-resume]')!;
  const complete = actions.querySelector<HTMLButtonElement>('[data-complete]')!;
  const edit = actions.querySelector<HTMLButtonElement>('[data-edit]')!;
  const reload = panel.querySelector<HTMLButtonElement>('[data-reload]')!;
  let snapshot: SessionGoalSnapshot | null = null;
  let editing = Boolean(objective);
  let busy = false;
  input.value = objective;
  const inputLength = () => Array.from(input.value.trim()).length;
  const refreshSave = () => {
    const length = inputLength();
    const tooLong = length > MAX_OBJECTIVE;
    input.setAttribute('aria-invalid', String(tooLong));
    validation.textContent = tooLong ? `目标最多 ${MAX_OBJECTIVE} 个字符` : '';
    save.disabled = busy || !snapshot || length < 1 || length > MAX_OBJECTIVE;
  };
  const render = () => {
    const goal = snapshot?.goal;
    title.textContent = goal ? `会话目标 · ${STATUS[goal.status]}` : '会话目标';
    displayed.hidden = !goal;
    displayed.textContent = goal?.objective ?? '';
    form.hidden = !editing && snapshot?.goal !== null;
    input.readOnly = busy;
    cancelEdit.hidden = !goal;
    actions.hidden = !goal || !form.hidden;
    pause.hidden = goal?.status !== 'active';
    resume.hidden = goal?.status !== 'paused';
    complete.hidden = !goal || goal.status === 'completed';
    for (const button of [cancelEdit, pause, resume, complete, edit]) button.disabled = busy;
    reload.hidden = busy || Boolean(snapshot);
    refreshSave();
  };
  const restoreActionFocus = (previous: Element | null) => {
    const focused = document.activeElement;
    if (!previous || !panel.contains(previous) || (focused !== previous && focused !== document.body)) return;
    const next = [reload, ...(!form.hidden ? [input] : [pause, resume, edit])].find((element) => !element.hidden);
    next?.focus();
  };
  const problem = (error: unknown) => error instanceof Error ? error.message : '网关未返回可确认的结果';
  const report = (action: SessionGoalAction | 'read', operationId: string, started: number, result: string) => {
    logStream('session-goal', 'request-settled', { action, operationId, result, durationMs: performance.now() - started });
  };
  const load = async (operationId: string, onLoaded = '') => {
    const started = performance.now();
    try {
      const latest = await backendApi.sessionGoal(sessionId, signal);
      if (!isCurrent()) return;
      snapshot = latest;
      feedback.textContent = onLoaded;
      report('read', operationId, started, 'ok');
    } catch (error) {
      if (!isCurrent()) return;
      feedback.textContent = `无法读取目标：${problem(error)}`;
      report('read', operationId, started, 'failed');
    }
  };
  const read = async () => {
    if (!isCurrent() || busy) return;
    const previous = document.activeElement;
    busy = true;
    feedback.textContent = '正在读取目标…';
    render();
    await load(crypto.randomUUID());
    if (!isCurrent()) return;
    busy = false;
    render();
    if (previous === reload) restoreActionFocus(previous);
    else if (!form.hidden && document.activeElement === close) input.focus();
  };
  const apply = async (action: SessionGoalAction) => {
    if (!isCurrent() || busy || !snapshot || (action === 'set' && save.disabled)) return;
    const previous = document.activeElement;
    const operationId = crypto.randomUUID();
    const started = performance.now();
    const data = {
      version: snapshot.version, action, ...(action === 'set' ? { objective: input.value.trim() } : {}),
    };
    busy = true;
    feedback.textContent = '正在更新目标…';
    render();
    try {
      const changed = await backendApi.changeSessionGoal(sessionId, data, signal);
      if (!isCurrent()) return;
      if (changed) {
        snapshot = changed;
        if (action === 'set') editing = false;
        feedback.textContent = '目标已更新';
        report(action, operationId, started, 'applied');
      } else {
        snapshot = null;
        report(action, operationId, started, 'stale');
        feedback.textContent = '正在读取最新目标…';
        await load(operationId, '目标或会话已更新。请核对当前状态后再提交。');
      }
    } catch (error) {
      if (!isCurrent()) return;
      snapshot = null; // A lost reply is not proof that the write failed. Require a read before trying again.
      feedback.textContent = `未能确认目标更新。请重新读取：${problem(error)}`;
      report(action, operationId, started, 'unconfirmed');
    } finally {
      if (isCurrent()) {
        busy = false;
        render();
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
    controller.abort();
    for (const unbind of unsubscribe) unbind();
    panel.remove();
  };
  host.prepend(panel);
  void read();
  (objective ? input : close).focus();
  return true;
}
