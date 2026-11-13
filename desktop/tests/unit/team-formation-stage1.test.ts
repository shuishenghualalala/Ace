/** @vitest-environment happy-dom */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { backendApi } from '../../src/ui/backend-client';
import { initAgentsPage, loadAgentsPage } from '../../src/ui/features/agents-page';
import { __resetAllStoresForTest, configStore } from '../../src/ui/stores/stores';

beforeEach(() => {
  __resetAllStoresForTest();
  document.body.innerHTML = '<div id="agents-page-root"></div>';
  localStorage.clear();
  configStore.set({ config: {
    model: 'test', has_key: true, base_url: '', active_model_id: 'test', models: [],
    external_agents: { enabled: true },
  } });
  vi.spyOn(backendApi, 'scanRuntimes').mockResolvedValue([]);
  vi.spyOn(backendApi, 'runtimes').mockResolvedValue([]);
  vi.spyOn(backendApi, 'externalAgents').mockResolvedValue([]);
  vi.spyOn(backendApi, 'externalTeams').mockResolvedValue([]);
  vi.spyOn(backendApi, 'externalTeamRoles').mockResolvedValue([]);
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
  delete (window as Window & { Crew?: unknown }).Crew;
});

describe('team formation goal', () => {
  it('keeps the original duty and pending role when a late formation result arrives', async () => {
    vi.mocked(backendApi.externalAgents).mockResolvedValue([{ id: 'a', name: '成员 A', provider: 'kimi', runtime_id: 'rt', model: 'm' }]);
    vi.mocked(backendApi.externalTeamRoles).mockResolvedValue([
      { key: 'project_manager', label: '项目经理', description: '', capabilities: [], workflow_lane: 'plan' },
      { key: 'developer', label: '开发', description: '', capabilities: [], workflow_lane: 'build' },
      { key: 'reviewer', label: '审查', description: '', capabilities: ['review'], workflow_lane: 'verify' },
    ]);
    const draft = { leader_agent_id: 'crew::builtin', workflow: '', members: [
      { agent_id: 'crew::builtin', role: '协调', role_key: 'project_manager' },
      { agent_id: 'a', role: '原职责', role_key: 'developer' },
    ] } as Awaited<ReturnType<typeof backendApi.suggestExternalTeamAuto>>;
    const late = { ...draft, members: [draft.members[0], { agent_id: 'a', role: '迟到的职责', role_key: 'reviewer' }] };
    let finish!: (value: typeof draft) => void;
    vi.spyOn(backendApi, 'suggestExternalTeamAuto').mockImplementation((_payload, options) => {
      options?.onSuggestion?.(draft, 'fast');
      return new Promise((resolve) => { finish = resolve; });
    });
    await initAgentsPage(); await loadAgentsPage();
    document.querySelector<HTMLElement>('[data-agents-tab="create-team"]')?.click();
    const name = document.querySelector<HTMLInputElement>('#team-name-input')!;
    name.value = '团队'; name.dispatchEvent(new Event('input', { bubbles: true }));
    document.querySelector<HTMLElement>('[data-suggest-team]')!.click();
    document.querySelector<HTMLElement>('[data-agents-select-key="member-role:a"]')!.click();
    document.querySelector<HTMLElement>('[data-agents-select-value="reviewer"]')!.click();
    finish(late);
    await vi.waitFor(() => expect(document.querySelector<HTMLButtonElement>('[data-recheck-formation]')?.disabled).toBe(false));
    expect(document.querySelector<HTMLButtonElement>('[data-create-team]')?.disabled).toBe(true);
    expect(document.querySelector<HTMLTextAreaElement>('[data-member-role="a"]')?.value).toBe('原职责');
    expect(document.querySelector('[data-agents-select-key="member-role:a"]')?.textContent).toContain('审查');
    document.querySelector<HTMLElement>('[data-cancel-team]')?.click();
  });

  it.each(['unknown', 'missing', 'unavailable', 'ai_failure'] as const)(
    'handles role-change assessment: %s', async (scenario) => {
      vi.mocked(backendApi.externalAgents).mockResolvedValue([{ id: 'a', name: '成员 A', provider: 'kimi', runtime_id: 'rt', model: 'm' }]);
      vi.mocked(backendApi.externalTeamRoles).mockResolvedValue([
        { key: 'project_manager', label: '项目经理', description: '', capabilities: [], workflow_lane: 'plan' },
        { key: 'developer', label: '开发', description: '', capabilities: [], workflow_lane: 'build' },
        { key: 'reviewer', label: '审查', description: '', capabilities: ['review'], workflow_lane: 'verify' },
      ]);
      const suggestion = { leader_agent_id: 'crew::builtin', workflow: '', members: [
        { agent_id: 'crew::builtin', role: '协调', role_key: 'project_manager' },
        { agent_id: 'a', role: '原职责', role_key: 'developer' },
      ] } as Awaited<ReturnType<typeof backendApi.suggestExternalTeamAuto>>;
      vi.spyOn(backendApi, 'suggestExternalTeamAuto').mockImplementation(async (_payload, options) => {
        options?.onSuggestion?.(suggestion, 'final'); return suggestion;
      });
      const create = vi.spyOn(backendApi, 'createExternalTeam').mockResolvedValue({ id: 'team', name: '团队' });
      vi.spyOn(backendApi, 'assessExternalTeam').mockImplementation(async (payload) => ({
        coverage: { required: ['review'], covered: [], uncovered: scenario === 'missing' ? ['review'] : [],
          unverified: scenario === 'unknown' ? ['review'] : [], unavailable: scenario === 'unavailable' ? ['review'] : [] },
        capability_evidence_confidence: 0,
        members: payload.members.map((item) => ({ agent_id: item.agent_id, role_key: item.role_key,
          available: item.agent_id !== 'a' || scenario !== 'unavailable', coverage: {
            required: ['review'], covered: [], missing: item.agent_id === 'a' && scenario === 'missing' ? ['review'] : [],
            unknown: scenario === 'unknown' ? ['review'] : [], unavailable: item.agent_id === 'a' && scenario === 'unavailable' ? ['review'] : [],
          } })),
        role_suggestions: scenario === 'ai_failure' || scenario === 'unavailable' ? [] : [
          { agent_id: 'a', role_key: 'reviewer', responsibility: '独立核对证据与结论' },
        ],
        role_suggestion_error: scenario === 'unavailable' ? 'member_unavailable' : scenario === 'ai_failure' ? 'provider_error' : '',
      }));
      await initAgentsPage(); await loadAgentsPage();
      document.querySelector<HTMLElement>('[data-agents-tab="create-team"]')?.click();
      const name = document.querySelector<HTMLInputElement>('#team-name-input')!;
      name.value = '团队'; name.dispatchEvent(new Event('input', { bubbles: true }));
      document.querySelector<HTMLElement>('[data-suggest-team]')!.click();
      await vi.waitFor(() => expect(document.querySelector('[data-return-team-setup]')).not.toBeNull());
      document.querySelector<HTMLElement>('[data-agents-select-key="member-role:a"]')!.click();
      document.querySelector<HTMLElement>('[data-agents-select-value="reviewer"]')!.click();
      expect(document.querySelector<HTMLTextAreaElement>('[data-member-role="a"]')?.value).toBe('原职责');
      expect(document.querySelector<HTMLButtonElement>('[data-create-team]')?.disabled).toBe(true);
      document.querySelector<HTMLElement>('[data-recheck-formation]')!.click();
      if (scenario === 'missing') {
        await vi.waitFor(() => expect(document.querySelector('[data-accept-role-risk="a"]')).not.toBeNull());
        expect(document.querySelector<HTMLTextAreaElement>('[data-member-role="a"]')?.value).toBe('原职责');
        document.querySelector<HTMLElement>('[data-accept-role-risk="a"]')!.click();
      } else if (scenario === 'unavailable') {
        await vi.waitFor(() => expect(document.querySelector('[data-role-risk="a"]')?.textContent).toContain('运行时或模型不可用'));
        expect(document.querySelector('[data-accept-role-risk="a"]')).toBeNull();
        document.querySelector<HTMLElement>('[data-revert-role-change="a"]')!.click();
        expect(document.querySelector<HTMLTextAreaElement>('[data-member-role="a"]')?.value).toBe('原职责');
      } else if (scenario === 'ai_failure') {
        await vi.waitFor(() => expect(document.querySelector('[data-assessment-result]')?.textContent).toContain('尚未生效'));
        expect(document.querySelector<HTMLTextAreaElement>('[data-member-role="a"]')?.value).toBe('原职责');
        expect(document.querySelector<HTMLButtonElement>('[data-create-team]')?.disabled).toBe(true);
        const duty = document.querySelector<HTMLTextAreaElement>('[data-member-role="a"]')!;
        duty.value = '用户手写审阅职责'; duty.dispatchEvent(new Event('input', { bubbles: true }));
        document.querySelector<HTMLElement>('[data-recheck-formation]')!.click();
        await vi.waitFor(() => expect(document.querySelector<HTMLButtonElement>('[data-use-manual-role]')?.disabled).toBe(false));
        document.querySelector<HTMLElement>('[data-use-manual-role]')!.click();
        expect(document.querySelector<HTMLTextAreaElement>('[data-member-role="a"]')?.value).toBe('用户手写审阅职责');
        expect(document.querySelector<HTMLButtonElement>('[data-create-team]')?.disabled).toBe(false);
      } else {
        await vi.waitFor(() => expect(document.querySelector<HTMLTextAreaElement>('[data-member-role="a"]')?.value).toBe('独立核对证据与结论'));
        document.querySelector<HTMLElement>('[data-create-team]')!.click();
        await vi.waitFor(() => expect(create).toHaveBeenCalledTimes(1));
        const saved = create.mock.calls[0][0].formation_plan?.members.find((item) => item.agent_id === 'a');
        expect(saved?.role_key).toBe('reviewer');
        expect(saved?.responsibility.mission).toBe('独立核对证据与结论');
        expect(saved?.responsibility_source).toBe('ai');
        await vi.waitFor(() => expect(document.querySelector('[data-return-team-setup]')).toBeNull());
      }
      if (scenario === 'missing') {
        expect(document.querySelector<HTMLTextAreaElement>('[data-member-role="a"]')?.value).toBe('独立核对证据与结论');
        document.querySelector<HTMLElement>('[data-create-team]')!.click();
        await vi.waitFor(() => expect(create).toHaveBeenCalledTimes(1));
        expect(create.mock.calls[0][0].members.find((item) => item.agent_id === 'a')?.role_key).toBe('reviewer');
        await vi.waitFor(() => expect(document.querySelector('[data-return-team-setup]')).toBeNull());
      }
      document.querySelector<HTMLElement>('[data-cancel-team]')?.click();
    },
  );

  it('shows a failed capability check beside the roster and allows retry', async () => {
    vi.mocked(backendApi.externalTeamRoles).mockResolvedValue([
      { key: 'project_manager', label: '项目经理', description: '', capabilities: [], workflow_lane: 'plan' },
      { key: 'reviewer', label: '审查员', description: '', capabilities: ['review'], workflow_lane: 'verify' },
    ]);
    const suggestion = { leader_agent_id: 'crew::builtin', workflow: '',
      members: [{ agent_id: 'crew::builtin', role: '协调', role_key: 'project_manager' }],
    } as Awaited<ReturnType<typeof backendApi.suggestExternalTeamAuto>>;
    vi.spyOn(backendApi, 'suggestExternalTeamAuto').mockImplementation(async (_payload, options) => {
      options?.onSuggestion?.(suggestion, 'final');
      return suggestion;
    });
    vi.spyOn(backendApi, 'assessExternalTeam').mockRejectedValue(new Error('服务不可用'));
    await initAgentsPage();
    await loadAgentsPage();
    document.querySelector<HTMLElement>('[data-agents-tab="create-team"]')?.click();
    const name = document.querySelector<HTMLInputElement>('#team-name-input')!;
    name.value = '测试团队';
    name.dispatchEvent(new Event('input', { bubbles: true }));
    document.querySelector<HTMLElement>('[data-suggest-team]')!.click();
    await vi.waitFor(() => expect(document.querySelector('[data-return-team-setup]')).not.toBeNull());
    document.querySelector<HTMLElement>('[data-agents-select-key="member-role:crew::builtin"]')!.click();
    document.querySelector<HTMLElement>('[data-agents-select-value="reviewer"]')!.click();
    document.querySelector<HTMLElement>('[data-recheck-formation]')!.click();
    await vi.waitFor(() => expect(document.querySelector('[data-assessment-result]')?.textContent).toContain('服务不可用'));
    expect(document.querySelector('[data-review-guidance]')?.hasAttribute('hidden')).toBe(false);
    expect(document.querySelector<HTMLButtonElement>('[data-recheck-formation]')?.disabled).toBe(false);
    document.querySelector<HTMLElement>('[data-cancel-team]')?.click();
  });

  it('does not apply an outdated capability check after the user changes a role', async () => {
    vi.mocked(backendApi.externalTeamRoles).mockResolvedValue([
      { key: 'project_manager', label: '项目经理', description: '', capabilities: [], workflow_lane: 'plan' },
      { key: 'reviewer', label: '审查员', description: '', capabilities: ['review'], workflow_lane: 'verify' },
    ]);
    const suggestion = {
      leader_agent_id: 'crew::builtin', workflow: '协调工作',
      members: [{ agent_id: 'crew::builtin', role: '协调', role_key: 'project_manager' }],
    } as Awaited<ReturnType<typeof backendApi.suggestExternalTeamAuto>>;
    vi.spyOn(backendApi, 'suggestExternalTeamAuto').mockImplementation(async (_payload, options) => {
      options?.onSuggestion?.(suggestion, 'final');
      return suggestion;
    });
    let finish!: (value: Awaited<ReturnType<typeof backendApi.assessExternalTeam>>) => void;
    vi.spyOn(backendApi, 'assessExternalTeam').mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
    await initAgentsPage();
    await loadAgentsPage();
    document.querySelector<HTMLElement>('[data-agents-tab="create-team"]')?.click();
    const name = document.querySelector<HTMLInputElement>('#team-name-input')!;
    name.value = '测试团队';
    name.dispatchEvent(new Event('input', { bubbles: true }));
    document.querySelector<HTMLElement>('[data-suggest-team]')!.click();
    await vi.waitFor(() => expect(document.querySelector('[data-return-team-setup]')).not.toBeNull());
    const select = (key: string) => {
      document.querySelector<HTMLElement>('[data-agents-select-key="member-role:crew::builtin"]')!.click();
      document.querySelector<HTMLElement>(`[data-agents-select-value="${key}"]`)!.click();
    };
    select('reviewer');
    document.querySelector<HTMLElement>('[data-recheck-formation]')!.click();
    select('project_manager');
    finish({ coverage: { required: ['review'], covered: ['review'], uncovered: [] }, capability_evidence_confidence: 1, members: [] });
    await vi.waitFor(() => expect(document.querySelector<HTMLButtonElement>('[data-create-team]')?.disabled).toBe(false));
    expect(document.querySelector('[data-review-guidance]')?.hasAttribute('hidden')).toBe(false);
    expect(document.querySelector('[data-coverage-metric]')?.textContent).toContain('待更新');
    expect(document.querySelector('[data-assessment-result]')?.textContent).toContain('分工发生变化');
    document.querySelector<HTMLElement>('[data-cancel-team]')?.click();
  });

  it('applies the organized goal directly, permits undo, and preserves later edits', async () => {
    let finish!: (draft: Awaited<ReturnType<typeof backendApi.draftExternalTeamDescription>>) => void;
    let latestOptions: Parameters<typeof backendApi.draftExternalTeamDescription>[1];
    vi.spyOn(backendApi, 'draftExternalTeamDescription').mockImplementation((_payload, options) => {
      latestOptions = options;
      return new Promise((resolve) => { finish = resolve; });
    });
    await initAgentsPage();
    await loadAgentsPage();
    document.querySelector<HTMLElement>('[data-agents-tab="create-team"]')?.click();
    const name = document.querySelector<HTMLInputElement>('#team-name-input')!;
    name.value = '测试团队';
    name.dispatchEvent(new Event('input', { bubbles: true }));
    let goal = document.querySelector<HTMLTextAreaElement>('#team-description-input')!;
    goal.value = '分析比赛要求并编写报告';
    goal.dispatchEvent(new Event('input', { bubbles: true }));
    document.querySelector<HTMLElement>('[data-organize-team-description]')!.click();
    const result = { description: '1. 分析比赛要求\n2. 编写报告' };
    latestOptions?.onDraft?.(result, 'optimized', {});
    finish(result);
    await vi.waitFor(() => expect(document.querySelector<HTMLTextAreaElement>('#team-description-input')?.value).toBe(result.description));
    expect(document.querySelector('[data-restore-original-description]')).not.toBeNull();
    document.querySelector<HTMLElement>('[data-restore-original-description]')!.click();
    expect(document.querySelector<HTMLTextAreaElement>('#team-description-input')?.value).toBe('分析比赛要求并编写报告');

    document.querySelector<HTMLElement>('[data-organize-team-description]')!.click();
    goal = document.querySelector<HTMLTextAreaElement>('#team-description-input')!;
    goal.value = '用户在整理时继续修改';
    goal.dispatchEvent(new Event('input', { bubbles: true }));
    latestOptions?.onDraft?.(result, 'optimized', {});
    finish(result);
    await Promise.resolve();
    expect(document.querySelector<HTMLTextAreaElement>('#team-description-input')?.value).toBe('用户在整理时继续修改');
    document.querySelector<HTMLElement>('[data-cancel-team]')?.click();
  });

  it('keeps the goal and shows an error when AI falls back', async () => {
    vi.spyOn(backendApi, 'draftExternalTeamDescription').mockImplementation(async (_payload, options) => {
      const draft = { description: '原始目标' };
      options?.onDraft?.(draft, 'fallback', {});
      return draft;
    });
    await initAgentsPage();
    await loadAgentsPage();
    document.querySelector<HTMLElement>('[data-agents-tab="create-team"]')?.click();
    const name = document.querySelector<HTMLInputElement>('#team-name-input')!;
    name.value = '测试团队';
    name.dispatchEvent(new Event('input', { bubbles: true }));
    const goal = document.querySelector<HTMLTextAreaElement>('#team-description-input')!;
    goal.value = '原始目标';
    goal.dispatchEvent(new Event('input', { bubbles: true }));
    document.querySelector<HTMLElement>('[data-organize-team-description]')!.click();
    await vi.waitFor(() => expect(document.querySelector('.team-create__goal-feedback')?.textContent).toContain('已保留原文'));
    expect(document.querySelector<HTMLTextAreaElement>('#team-description-input')?.value).toBe('原始目标');
    document.querySelector<HTMLElement>('[data-cancel-team]')?.click();
  });

  it('explains an unresolved AI role and lets the user retry without choosing a role', async () => {
    vi.mocked(backendApi.externalAgents).mockResolvedValue([
      { id: 'a', name: '成员 A', provider: 'kimi', runtime_id: 'rt', model: 'm' },
      { id: 'b', name: '成员 B', provider: 'kimi', runtime_id: 'rt', model: 'm' },
    ]);
    vi.mocked(backendApi.externalTeamRoles).mockResolvedValue([
      { key: 'project_manager', label: '项目经理', description: '', capabilities: [], workflow_lane: 'plan' },
      { key: 'unclassified', label: '待确认角色', description: '', capabilities: [], workflow_lane: 'other' },
      { key: 'independent_reviewer', label: '独立审阅', description: '', capabilities: [], workflow_lane: 'verify' },
    ]);
    const suggestion = {
      leader_agent_id: 'crew::builtin', workflow: '',
      selected_formation_mode: 'fast', requested_formation_mode: 'auto', fallback_reason: 'no_material_improvement',
      members: [
        { agent_id: 'crew::builtin', role: '协调', role_key: 'project_manager' },
        { agent_id: 'a', role: '待补充分工', role_key: 'unclassified' },
        { agent_id: 'b', role: '审查项目', role_key: 'independent_reviewer' },
      ],
    } as Awaited<ReturnType<typeof backendApi.suggestExternalTeamAuto>>;
    const suggest = vi.spyOn(backendApi, 'suggestExternalTeamAuto').mockImplementation(async (_payload, options) => {
      options?.onSuggestion?.(suggestion, 'final');
      return suggestion;
    });
    const create = vi.spyOn(backendApi, 'createExternalTeam').mockResolvedValue({ id: 'new-team', name: '测试团队' });
    await initAgentsPage();
    await loadAgentsPage();
    document.querySelector<HTMLElement>('[data-agents-tab="create-team"]')?.click();
    const name = document.querySelector<HTMLInputElement>('#team-name-input')!;
    name.value = '测试团队';
    name.dispatchEvent(new Event('input', { bubbles: true }));
    document.querySelector<HTMLElement>('[data-agents-select-key="team-required-agent"]')!.click();
    document.querySelector<HTMLElement>('[data-agents-select-value="a"]')!.click();
    const duty = document.querySelector<HTMLTextAreaElement>('[data-required-responsibility="a"]')!;
    duty.value = '负责独立审阅项目';
    duty.dispatchEvent(new Event('input', { bubbles: true }));
    document.querySelector<HTMLElement>('[data-suggest-team]')?.click();
    await vi.waitFor(() => expect(document.querySelector('[data-retry-roles]')).not.toBeNull());
    expect(document.querySelector('.formation-progress')?.classList.contains('formation-progress--ready_partial')).toBe(true);
    expect(document.querySelector('[data-retry-roles]')?.parentElement?.textContent).toContain('暂不设置仍可创建');
    expect(document.querySelector('[data-retry-roles]')?.parentElement?.textContent).toContain('成员 A');
    expect(document.querySelector('[data-retry-roles]')?.parentElement?.textContent).not.toContain('成员 B');
    expect(document.querySelector('[data-agents-select-key="member-role:a"]')?.textContent).toContain('待确认角色');
    expect(document.querySelector('[data-agents-select-key="member-role:b"]')?.textContent).toContain('独立审阅');
    document.querySelector<HTMLElement>('[data-retry-roles]')!.click();
    await vi.waitFor(() => expect(suggest).toHaveBeenCalledTimes(2));
    await vi.waitFor(() => expect(document.querySelector<HTMLButtonElement>('[data-create-team]')?.disabled).toBe(false));
    document.querySelector<HTMLElement>('[data-create-team]')?.click();
    await vi.waitFor(() => expect(create).toHaveBeenCalledTimes(1));
    expect(create.mock.calls[0][0].members.find((member) => member.agent_id === 'a')?.role_key).toBe('unclassified');
    expect(create.mock.calls[0][0].members.find((member) => member.agent_id === 'b')?.role_key).toBe('independent_reviewer');
  });

  it('updates the collaboration reference on role changes and preserves manual edits', async () => {
    const assess = vi.spyOn(backendApi, 'assessExternalTeam').mockImplementation(async (payload) => ({
      coverage: { required: [], covered: [], uncovered: [], unverified: [], unavailable: [] }, capability_evidence_confidence: 0,
      members: payload.members.map((item) => ({ agent_id: item.agent_id, role_key: item.role_key, available: true,
        coverage: { required: [], covered: [], missing: [], unknown: [], unavailable: [] } })),
      role_suggestions: (payload.role_changes || []).map((item) => ({ agent_id: item.agent_id, role_key: item.role_key, responsibility: '根据新角色处理团队任务' })),
    }));
    vi.mocked(backendApi.externalTeamRoles).mockResolvedValue([
      { key: 'project_manager', label: '项目经理', description: '', capabilities: [], workflow_lane: 'plan' },
      { key: 'reviewer', label: '审查员', description: '', capabilities: [], workflow_lane: 'review' },
    ]);
    const suggestion = {
      leader_agent_id: 'crew::builtin', workflow: '原始协作建议',
      members: [{ agent_id: 'crew::builtin', role: '协调', role_key: 'project_manager' }],
    } as Awaited<ReturnType<typeof backendApi.suggestExternalTeamAuto>>;
    const suggest = vi.spyOn(backendApi, 'suggestExternalTeamAuto').mockImplementation(async (_payload, options) => {
      options?.onSuggestion?.(suggestion, 'final');
      return suggestion;
    });
    await initAgentsPage();
    await loadAgentsPage();
    document.querySelector<HTMLElement>('[data-agents-tab="create-team"]')?.click();
    const name = document.querySelector<HTMLInputElement>('#team-name-input')!;
    name.value = '测试团队';
    name.dispatchEvent(new Event('input', { bubbles: true }));
    document.querySelector<HTMLElement>('[data-suggest-team]')?.click();
    await vi.waitFor(() => expect(document.querySelector('[data-return-team-setup]')).not.toBeNull());
    expect(document.querySelectorAll('[data-create-team]')).toHaveLength(1);
    expect(document.querySelector('[data-create-team]')?.textContent).toContain('确认并创建团队');
    const roleDetails = document.querySelector<HTMLDetailsElement>('[data-member-role-details]')!;
    expect(roleDetails.open).toBe(false);
    expect(document.querySelector('[data-responsibility-summary]')?.textContent).toBe('协调');
    roleDetails.open = true;

    const selectRole = (role: string) => {
      document.querySelector<HTMLElement>('[data-agents-select-key="member-role:crew::builtin"]')!.click();
      document.querySelector<HTMLElement>(`[data-agents-select-value="${role}"]`)!.click();
    };
    selectRole('reviewer');
    expect(document.querySelector('[data-review-guidance]')?.hasAttribute('hidden')).toBe(false);
    expect(document.querySelector('[data-review-guidance]')?.textContent).toContain('新角色待检查');
    expect(document.querySelector('[data-suggest-team]')).toBeNull();
    expect(document.querySelector<HTMLDetailsElement>('[data-member-role-details]')?.open).toBe(true);
    expect(document.querySelector<HTMLButtonElement>('[data-create-team]')?.disabled).toBe(true);
    document.querySelector<HTMLElement>('[data-recheck-formation]')?.click();
    await vi.waitFor(() => expect(document.querySelector<HTMLButtonElement>('[data-create-team]')?.disabled).toBe(false));
    let workflow = document.querySelector<HTMLTextAreaElement>('#team-workflow-input')!;
    expect(workflow.value).toContain('审查员');
    expect(workflow.value).not.toContain('原始协作建议');
    workflow.value = '用户自定义协作说明';
    workflow.dispatchEvent(new Event('input', { bubbles: true }));
    selectRole('project_manager');
    workflow = document.querySelector<HTMLTextAreaElement>('#team-workflow-input')!;
    expect(workflow.value).toBe('用户自定义协作说明');
    const duty = document.querySelector<HTMLTextAreaElement>('[data-member-role]')!;
    duty.value = '用户修改的职责';
    duty.dispatchEvent(new Event('input', { bubbles: true }));
    document.querySelector<HTMLElement>('[data-recheck-formation]')?.click();
    await vi.waitFor(() => expect(document.querySelector<HTMLButtonElement>('[data-create-team]')?.disabled).toBe(false));
    expect(suggest).toHaveBeenCalledTimes(1);
    expect(assess).toHaveBeenCalledTimes(2);
    expect(assess.mock.calls[1][0].members).toContainEqual({ agent_id: 'crew::builtin', role_key: 'project_manager', assigned_capabilities: [] });
    expect(document.querySelector<HTMLTextAreaElement>('[data-member-role]')?.value).toBe('用户修改的职责');
    expect(document.querySelector('[data-assessment-result]')?.textContent).toContain('没有可检查的标准能力项');
    expect(document.querySelector('[data-assessment-result]')?.textContent).toContain('手写职责已保留');
    expect(document.querySelector('[data-apply-duty-suggestion]')).not.toBeNull();
    expect(document.querySelector('[data-workflow-note]')?.textContent).toContain('请检查');
    expect(document.querySelector<HTMLTextAreaElement>('#team-workflow-input')?.value).toBe('用户自定义协作说明');
    document.querySelector<HTMLElement>('[data-apply-duty-suggestion]')!.click();
    expect(document.querySelector<HTMLTextAreaElement>('[data-member-role]')?.value).toBe('根据新角色处理团队任务');
    document.querySelector<HTMLElement>('[data-restore-duty]')!.click();
    expect(document.querySelector<HTMLTextAreaElement>('[data-member-role]')?.value).toBe('用户修改的职责');
    document.querySelector<HTMLElement>('[data-cancel-team]')?.click();
  });

  it('shows the gateway error returned by a streamed formation request', async () => {
    type Event = { request_id: string; type: 'head' | 'chunk' | 'end'; status?: number; text?: string };
    let listener: ((event: Event) => void) | null = null;
    Object.defineProperty(window, 'Crew', { configurable: true, value: {
      onGatewayStreamEvent: (callback: (event: Event) => void) => {
        listener = callback;
        return () => { listener = null; };
      },
      gatewayStreamCancel: vi.fn(async () => ({ ok: true })),
      gatewayStreamStart: async (requestId: string) => {
        await Promise.resolve();
        listener?.({ request_id: requestId, type: 'head', status: 409 });
        listener?.({ request_id: requestId, type: 'chunk', text: JSON.stringify({ error: '请先配置可用模型' }) });
        listener?.({ request_id: requestId, type: 'end' });
        return { ok: false };
      },
    } });
    await expect(backendApi.suggestExternalTeamAuto({ name: '团队' })).rejects.toThrow('请先配置可用模型');
  });

  it('keeps a generated goal as a suggestion until the user accepts it', async () => {
    vi.spyOn(backendApi, 'draftExternalTeamDescription').mockImplementation(async (_payload, options) => {
      const draft = { description: 'AI 建议的目标' };
      options?.onDraft?.(draft, 'optimized', {});
      return draft;
    });
    await initAgentsPage();
    await loadAgentsPage();
    document.querySelector<HTMLElement>('[data-agents-tab="create-team"]')?.click();
    const name = document.querySelector<HTMLInputElement>('#team-name-input')!;
    name.value = '比赛团队';
    name.dispatchEvent(new Event('input', { bubbles: true }));
    expect(document.querySelector('[data-organize-team-description]')?.hasAttribute('hidden')).toBe(true);
    await vi.waitFor(() => {
      expect(document.querySelector('[data-generated-description]')?.hasAttribute('hidden')).toBe(false);
    }, { timeout: 2500 });
    expect(document.querySelector<HTMLTextAreaElement>('#team-description-input')?.value).toBe('');
    expect(document.querySelector('[data-generated-description]')?.textContent).toContain('AI 建议的目标');
    document.querySelector<HTMLElement>('[data-apply-generated-description]')?.click();
    expect(document.querySelector<HTMLTextAreaElement>('#team-description-input')?.value).toBe('AI 建议的目标');
    expect(document.querySelector('[data-organize-team-description]')?.hasAttribute('hidden')).toBe(true);
    const editedGoal = document.querySelector<HTMLTextAreaElement>('#team-description-input')!;
    editedGoal.value += '，增加验收标准';
    editedGoal.dispatchEvent(new Event('input', { bubbles: true }));
    expect(document.querySelector('[data-organize-team-description]')?.textContent).toContain('AI 帮我整理目标');
    expect(document.querySelector('[data-organize-team-description]')?.hasAttribute('hidden')).toBe(false);
  });

  it('reuses an unchanged team, permits clearing the goal, and cancels the draft', async () => {
    const suggestion = {
      leader_agent_id: 'crew::builtin', workflow: '协调工作',
      members: [{ agent_id: 'crew::builtin', role: '协调', role_key: 'project_manager' }],
      formation_plan: {
        version: 1, leader_agent_id: 'crew::builtin', members: [],
        coverage: { required: [], covered: [], uncovered: [] },
        confidence: { requirement: 1, capability_evidence: 0, coverage: 0, overall: 0 },
        staffing_mode: 'minimal', excluded_agent_ids: [], reasons: [], warnings: [],
      },
    } as Awaited<ReturnType<typeof backendApi.suggestExternalTeamAuto>>;
    const suggest = vi.spyOn(backendApi, 'suggestExternalTeamAuto').mockImplementation(async (_payload, options) => {
      options?.onSuggestion?.(suggestion, 'final');
      return suggestion;
    });
    await initAgentsPage();
    await loadAgentsPage();
    document.querySelector<HTMLElement>('[data-agents-tab="create-team"]')?.click();
    const name = document.querySelector<HTMLInputElement>('#team-name-input')!;
    name.value = '项目开发小队';
    name.dispatchEvent(new Event('input', { bubbles: true }));
    const goal = document.querySelector<HTMLTextAreaElement>('#team-description-input')!;
    goal.focus();
    goal.value = '需要删除的目标';
    goal.dispatchEvent(new Event('input', { bubbles: true }));
    document.querySelector<HTMLElement>('[data-suggest-team]')?.click();
    await vi.waitFor(() => expect(document.querySelector('[data-return-team-setup]')).not.toBeNull());
    document.querySelector<HTMLElement>('[data-return-team-setup]')?.click();
    expect(document.querySelector('[data-view-existing-team]')?.textContent).toContain('查看已有阵容');
    document.querySelector<HTMLElement>('[data-view-existing-team]')?.click();
    expect(suggest).toHaveBeenCalledTimes(1);
    document.querySelector<HTMLElement>('[data-return-team-setup]')?.click();
    const returnedGoal = document.querySelector<HTMLTextAreaElement>('#team-description-input')!;
    returnedGoal.focus();
    returnedGoal.value = '';
    returnedGoal.dispatchEvent(new Event('input', { bubbles: true }));
    expect(returnedGoal.value).toBe('');
    expect(document.querySelector('[data-view-existing-team]')?.textContent).toContain('智能组队');
    document.querySelector<HTMLElement>('[data-view-existing-team]')?.click();
    await vi.waitFor(() => expect(suggest).toHaveBeenCalledTimes(2));
    expect(suggest.mock.calls[1]?.[0].description).toBe('');
    document.querySelector<HTMLElement>('[data-cancel-team]')?.click();
    document.querySelector<HTMLElement>('[data-agents-tab="create-team"]')?.click();
    expect(document.querySelector<HTMLInputElement>('#team-name-input')?.value).toBe('');
    expect(document.querySelector<HTMLTextAreaElement>('#team-description-input')?.value).toBe('');
    expect(suggest).toHaveBeenCalledTimes(2);
  });

  it('cancels an in-flight formation and ignores its late result', async () => {
    let finish!: (value: Awaited<ReturnType<typeof backendApi.suggestExternalTeamAuto>>) => void;
    let signal: AbortSignal | undefined;
    vi.spyOn(backendApi, 'suggestExternalTeamAuto').mockImplementation((_payload, options) => {
      signal = options?.signal;
      return new Promise((resolve) => { finish = resolve; });
    });
    await initAgentsPage();
    await loadAgentsPage();
    document.querySelector<HTMLElement>('[data-agents-tab="create-team"]')?.click();
    const name = document.querySelector<HTMLInputElement>('#team-name-input')!;
    name.value = '旧草稿';
    name.dispatchEvent(new Event('input', { bubbles: true }));
    document.querySelector<HTMLElement>('[data-suggest-team]')?.click();
    expect(signal?.aborted).toBe(false);
    document.querySelector<HTMLElement>('[data-cancel-team]')?.click();
    expect(signal?.aborted).toBe(true);
    finish({ leader_agent_id: 'crew::builtin', workflow: '', members: [] } as Awaited<ReturnType<typeof backendApi.suggestExternalTeamAuto>>);
    await Promise.resolve();
    document.querySelector<HTMLElement>('[data-agents-tab="create-team"]')?.click();
    expect(document.querySelector<HTMLInputElement>('#team-name-input')?.value).toBe('');
    expect(document.querySelector('[data-return-team-setup]')).toBeNull();
  });
});
