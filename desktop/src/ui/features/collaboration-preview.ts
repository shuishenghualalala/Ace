/** Planning preview is deliberately separate from the running team workflow. */
import { backendApi } from '../backend-client';
import { escapeHtml, state } from '../state';

export interface CollaborationPreview {
  task: { task_id: string; goal: string; team: { members: { member_id: string; name: string }[] } };
  state: string;
  error: string;
  plan: null | {
    work_units: { unit_id: string; objective: string; expected_outputs: string[]; completion_criteria: string[]; depends_on: string[]; required_tools: string[] }[];
    assignments: { unit_id: string; member_id: string; reason: string; responsibility_ref: string }[];
    decisions: { decision_id: string; unit_id: string; question: string; state: string; options: { member_id: string; name: string }[] }[];
  };
}

interface PreviewState { teamId: string; open: boolean; goal: string; loading: boolean; error: string; result: CollaborationPreview | null }
const previews = new Map<string, PreviewState>();
function current(sessionId: string): PreviewState {
  let value = previews.get(sessionId);
  const teamId = state.activeExternalTeamIdBySession[sessionId] || '';
  if (!value || value.teamId !== teamId) {
    value = { teamId, open: false, goal: '', loading: false, error: '', result: null };
    previews.set(sessionId, value);
  }
  return value;
}
function refresh(sessionId: string): void {
  window.dispatchEvent(new CustomEvent('team-collaboration:updated', { detail: { sessionId } }));
}
function e(value: unknown): string { return escapeHtml(String(value ?? '')); }

export function renderCollaborationPreview(sessionId: string): string {
  const teamId = state.activeExternalTeamIdBySession[sessionId] || '';
  if (!teamId) return '';
  const item = current(sessionId);
  const plan = item.result?.plan;
  const units = plan?.work_units || [];
  const names = new Map((item.result?.task.team.members || []).map((member) => [member.member_id, member.name]));
  const titles = new Map(units.map((unit, index) => [unit.unit_id, `工作 ${index + 1}`]));
  const work = units.map((unit, index) => {
    const assignment = plan?.assignments.find((a) => a.unit_id === unit.unit_id);
    const decision = plan?.decisions.find((d) => d.unit_id === unit.unit_id && d.state === 'open');
    return `<li><strong>工作 ${index + 1} · ${e(unit.objective)}</strong>
      <p>交付：${e(unit.expected_outputs.join('、'))}</p>
      <p>完成条件：${e(unit.completion_criteria.join('、'))}</p>
      <p>依赖：${unit.depends_on.length ? e(unit.depends_on.map((id) => titles.get(id) || '未知工作').join('、')) : '无前置工作'}</p>
      ${unit.required_tools.length ? `<p>所需工具：${e(unit.required_tools.join('、'))}（待执行时核实）</p>` : ''}
      ${assignment ? `<p>负责人：${e(names.get(assignment.member_id) || assignment.member_id)} · ${e(assignment.reason)}</p>` : ''}
      ${assignment?.responsibility_ref ? `<p>依据原文：${e(assignment.responsibility_ref)}</p>` : ''}
      ${decision ? `<p>${e(decision.question)}</p><select data-preview-decision="${e(decision.decision_id)}" aria-label="选择负责人"><option value="">选择负责人</option>${decision.options.map((option) => `<option value="${e(option.member_id)}">${e(option.name)}</option>`).join('')}</select> <button class="mini-cancel" type="button" data-preview-answer="${e(decision.decision_id)}">确认</button>` : ''}
    </li>`;
  }).join('');
  return `<section class="collaboration-preview"><button type="button" class="mini-cancel" data-preview-toggle aria-expanded="${item.open}">${item.open ? '收起规划预览' : '预览任务分工'}</button>
    ${item.open ? `<p>输入任务，查看团队可能如何拆分和分工。预览不会执行任务。</p>
    <textarea data-preview-goal rows="3" aria-label="预览的任务" placeholder="例如：分析项目功能并整理比赛材料">${e(item.goal)}</textarea>
    <button type="button" class="mini-cancel" data-preview-create ${item.loading ? 'disabled' : ''}>${item.loading ? '正在规划…' : '生成规划预览'}</button>
    ${item.error ? `<p role="alert">${e(item.error)}</p>` : ''}
    ${item.result?.state === 'failed' ? `<p role="alert">规划失败：${e(item.result.error)}</p>` : ''}
    ${plan ? `<p>规划预览 · ${units.length} 项工作${item.result?.state === 'waiting_user' ? ' · 有待确认的负责人' : ''}</p><ol>${work}</ol>` : ''}` : ''}
  </section>`;
}

export function activateCollaborationPreview(sessionId: string): void {
  const item = current(sessionId);
  document.querySelector<HTMLButtonElement>('[data-preview-toggle]')?.addEventListener('click', () => {
    item.open = !item.open;
    refresh(sessionId);
  });
  document.querySelector<HTMLTextAreaElement>('[data-preview-goal]')?.addEventListener('input', (event) => {
    item.goal = (event.currentTarget as HTMLTextAreaElement).value;
  });
  document.querySelector<HTMLButtonElement>('[data-preview-create]')?.addEventListener('click', () => {
    const teamId = state.activeExternalTeamIdBySession[sessionId] || '';
    const goal = item.goal.trim();
    if (!teamId || !goal) { item.error = '请先选择团队并填写任务。'; refresh(sessionId); return; }
    item.loading = true;
    item.error = '';
    item.result = null;
    refresh(sessionId);
    void backendApi.createCollaborationPreview(teamId, sessionId, goal, crypto.randomUUID()).then((response) => {
      item.result = response.preview;
    }).catch((error: unknown) => {
      item.error = error instanceof Error ? error.message : String(error);
    }).finally(() => { item.loading = false; refresh(sessionId); });
  });
  document.querySelectorAll<HTMLButtonElement>('[data-preview-answer]').forEach((button) => {
    button.addEventListener('click', () => {
      const decisionId = button.dataset.previewAnswer || '';
      const select = Array.from(document.querySelectorAll<HTMLSelectElement>('[data-preview-decision]'))
        .find((candidate) => candidate.dataset.previewDecision === decisionId);
      if (!select?.value || !item.result) { item.error = '请选择负责人。'; refresh(sessionId); return; }
      button.disabled = true;
      void backendApi.answerCollaborationPreview(item.result.task.task_id, decisionId, select.value).then((response) => {
        item.result = response.preview;
        item.error = '';
      }).catch((error: unknown) => { item.error = error instanceof Error ? error.message : String(error); })
        .finally(() => refresh(sessionId));
    });
  });
}
