/**
 * @vitest-environment happy-dom
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { backendApi } from '../../src/ui/backend-client';
import { initAgentsPage, loadAgentsPage } from '../../src/ui/features/agents-page';
import { bindComposerToolbar } from '../../src/ui/features/composer-toolbar';
import { STORAGE_KEYS } from '../../src/shared/storage-keys';
import {
  __resetAllStoresForTest,
  configStore,
} from '../../src/ui/stores/stores';

function mountComposer(): void {
  document.body.innerHTML = `
    <button type="button" id="chat-craft-btn">
      <span id="chat-craft-btn-label">智能体</span>
    </button>
    <span id="chat-model-picker-inline-label"></span>
  `;
  bindComposerToolbar();
}

async function openExternalEntry(): Promise<void> {
  document.getElementById('chat-craft-btn')?.click();
  const entry = document.querySelector<HTMLElement>('[data-craft-mode="external"]');
  expect(entry?.querySelector('.composer-external-agent-logo')).not.toBeNull();
  entry?.click();
  await vi.waitFor(() => {
    expect(document.getElementById('chat-external-inline-popover')?.textContent).not.toContain(
      '正在加载外援',
    );
  });
}

function rect(left: number, top: number, width: number, height: number): DOMRect {
  return {
    x: left,
    y: top,
    left,
    top,
    width,
    height,
    right: left + width,
    bottom: top + height,
    toJSON: () => ({}),
  } as DOMRect;
}

beforeEach(() => {
  __resetAllStoresForTest();
  document.body.innerHTML = '';
  localStorage.clear();
  vi.spyOn(backendApi, 'scanRuntimes').mockResolvedValue([]);
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('composer 外援入口', () => {
  it('外援中心用三步 Spotlight 引导发现、添加和派活，并支持关闭后从头重播', async () => {
    configStore.set({
      config: {
        model: 'test',
        has_key: true,
        base_url: '',
        active_model_id: 'test',
        models: [],
        external_agents: { enabled: true },
      },
    });
    vi.spyOn(backendApi, 'runtimes').mockResolvedValue([
      {
        id: 'runtime-codex',
        name: 'Codex',
        provider: 'codex',
        version: '1.2.3',
        executable_path: '/usr/local/bin/codex',
        availability_status: 'ready',
        metadata: {
          adapter_id: 'codex-acp',
          resolution_source: 'path',
          probe_latency_ms: 18,
          runtime_capabilities: {
            streaming: true,
            session_resume: true,
            tool_events: true,
          },
          default_model_id: 'gpt-test',
          models: [{ id: 'gpt-test', label: 'GPT Test', default: true }],
        },
      },
    ]);
    vi.spyOn(backendApi, 'externalAgents').mockResolvedValue([]);
    vi.spyOn(backendApi, 'externalTeams').mockResolvedValue([]);
    vi.spyOn(backendApi, 'externalTeamRoles').mockResolvedValue([]);

    document.body.innerHTML = '<div id="agents-page-root"></div>';
    await initAgentsPage();
    await loadAgentsPage();

    expect(document.querySelector('.agents-panel__head h1')?.textContent).toBe('外援中心');
    expect(document.querySelector('.agents-tabs')?.textContent).toContain('我的阵容');
    expect(document.querySelector('.agents-tabs')?.textContent).toContain('发现外援');
    expect(document.querySelector('.agents-tabs')?.textContent).toContain('添加外援');
    expect(document.querySelector('.agents-tabs')?.textContent).toContain('组建团队');
    const lineupEmptyStates = document.querySelectorAll('.agents-empty--actionable');
    expect(lineupEmptyStates).toHaveLength(2);
    lineupEmptyStates.forEach((emptyState) => {
      expect(emptyState.classList).toContain('agents-empty--wide');
      expect(emptyState.classList).not.toContain('agents-empty--plain');
    });
    expect(document.querySelector('[data-agents-guide]')?.textContent).toContain('第一次来外援中心');
    expect(document.querySelector('[data-agents-guide]')?.parentElement).toBe(
      document.querySelector('[data-agents-guide-portal]'),
    );
    expect(document.querySelector('[data-agents-guide-portal]')?.parentElement).toBe(document.body);
    expect(document.querySelector('[data-agents-guide-portal]')?.getAttribute('data-guide-mode')).toBe('welcome');
    expect(document.querySelector('[data-agents-guide-mask]')).toBeNull();

    document.querySelector<HTMLElement>('[data-agents-guide-start]')?.click();
    expect(document.querySelector('[data-agents-guide]')?.textContent).toContain('1/3');
    expect(document.querySelector('[data-agents-guide]')?.textContent).toContain('再找找');
    const firstTarget = document.querySelector<HTMLElement>('[data-scan-runtimes]');
    expect(firstTarget?.classList).toContain('agents-guide-target');
    const guidePortal = document.querySelector<HTMLElement>('[data-agents-guide-portal]');
    const guideBubble = document.querySelector<HTMLElement>('[data-agents-guide]');
    const guideHighlight = document.querySelector<HTMLElement>('[data-agents-guide-highlight]');
    expect(guidePortal?.dataset.guideMode).toBe('tour');
    expect(guidePortal?.classList).toContain('agents-guide-portal--tour');
    expect(document.querySelector('[data-agents-guide-mask]')).not.toBeNull();
    expect(guideHighlight).not.toBeNull();

    if (firstTarget && guideBubble) {
      firstTarget.scrollIntoView = vi.fn();
      vi.spyOn(firstTarget, 'getBoundingClientRect').mockReturnValue(rect(820, 160, 120, 40));
      vi.spyOn(guideBubble, 'getBoundingClientRect').mockReturnValue(rect(0, 0, 300, 160));
      document.querySelector<HTMLElement>('[data-agents-guide-locate]')?.click();
      await vi.waitFor(() => expect(guideHighlight?.hidden).toBe(false));
      expect(guideHighlight?.style.left).toBe('814px');
      expect(guideHighlight?.style.top).toBe('154px');
      expect(guideHighlight?.style.width).toBe('132px');
      expect(guideBubble.style.left).toBe('508px');
      expect(firstTarget.scrollIntoView).toHaveBeenCalledWith({
        behavior: 'smooth',
        block: 'center',
        inline: 'nearest',
      });
    }

    const scrollBy = vi.fn();
    const agentsPanel = document.querySelector<HTMLElement>('.agents-panel');
    if (agentsPanel) agentsPanel.scrollBy = scrollBy;
    guidePortal?.dispatchEvent(new WheelEvent('wheel', { deltaY: 72, bubbles: true, cancelable: true }));
    expect(scrollBy).toHaveBeenCalledWith({ top: 72, behavior: 'auto' });
    expect(guidePortal?.classList).not.toContain('is-inline');

    document.querySelector<HTMLElement>('[data-agents-guide-next]')?.click();
    expect(document.querySelector('[data-agents-guide-portal]')).toBe(guidePortal);
    expect(document.querySelector('[data-agents-guide]')).toBe(guideBubble);
    expect(document.querySelector('[data-agents-guide]')?.textContent).toContain('2/3');
    expect(document.querySelector('.agents-section h2')?.textContent).toBe('添加外援');
    expect(document.querySelector('[data-agents-select-key="agent-runtime"]')?.classList).toContain('agents-guide-target');

    document.querySelector<HTMLElement>('[data-agents-guide-next]')?.click();
    expect(document.querySelector('[data-agents-guide]')?.textContent).toContain('3/3');
    expect(document.querySelector('[data-agents-guide]')?.textContent).toContain('外援到位后');
    expect(document.querySelector('[data-agents-tab="create-agent"]')?.classList).toContain('agents-guide-target');

    document.querySelector<HTMLElement>('[data-agents-guide-next]')?.click();
    expect(document.querySelector('[data-agents-guide]')).toBeNull();
    expect(document.querySelector('[data-agents-guide-mask]')).toBeNull();
    expect(document.querySelector('.agents-guide-target')).toBeNull();
    expect(localStorage.getItem(STORAGE_KEYS.externalAgentsGuideDismissed)).toBe('true');

    document.querySelector<HTMLElement>('[data-agents-guide-open]')?.click();
    expect(document.querySelector('[data-agents-guide]')?.textContent).toContain('1/3');
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    expect(document.querySelector('[data-agents-guide]')).toBeNull();

    document.querySelector<HTMLElement>('[data-agents-tab="runtime"]')?.click();
    expect(document.querySelector('.agents-section__intro h2')?.textContent).toBe('发现外援');
    expect(document.querySelector('[data-scan-runtimes]')?.textContent).toContain('再找找');
    expect(document.querySelector('.agent-card__meta')?.textContent).toContain('随时可用');
    expect(document.querySelector('.runtime-technical-details')).toBeNull();
    expect(document.querySelector('.agent-card')?.textContent).not.toContain('codex-acp');
    expect(document.querySelector('.agent-card')?.textContent).not.toContain('/usr/local/bin/codex');
    expect(document.querySelector('[data-runtime-id]')?.textContent).toContain('使用');

    document.querySelector<HTMLElement>('[data-runtime-id="runtime-codex"]')?.click();
    expect(document.querySelector('.agents-section h2')?.textContent).toBe('添加外援');
    expect(document.querySelector('.agents-form')?.textContent).toContain('可用外援');
    expect(document.querySelector('.agents-form')?.textContent).toContain('外援称呼');
    expect(document.querySelector('.agents-form')?.textContent).toContain('使用模型');
    expect(document.querySelector('[data-create-agent]')?.textContent).toContain('加入我的外援');

    document.querySelector<HTMLElement>('[data-agents-tab="create-team"]')?.click();
    const leaderTrigger = document.querySelector<HTMLElement>('[data-agents-select-key="team-leader"]');
    expect(leaderTrigger?.textContent).toContain('Crew');
    expect(leaderTrigger?.textContent).toContain('内置');
    expect(leaderTrigger?.textContent).not.toContain('队长');
    document.querySelector<HTMLElement>('[data-toggle-team-constraints]')?.click();
    expect(document.querySelector('[data-agents-select-key="team-required-agent"]')?.textContent).toContain('选择外援');
    expect(document.querySelector('[data-agents-select-key="team-excluded-agent"]')?.textContent).toContain('选择外援');
    expect(document.querySelector('.team-constraints')?.textContent).not.toContain('选择 Agent');
  });

  it('首次目录为空时自动寻找一次，重复加载或已有目录都不会重复扫描', async () => {
    configStore.set({
      config: {
        model: 'test',
        has_key: true,
        base_url: '',
        active_model_id: 'test',
        models: [],
        external_agents: { enabled: true },
      },
    });
    const runtimes = vi.spyOn(backendApi, 'runtimes').mockResolvedValue([]);
    vi.spyOn(backendApi, 'externalAgents').mockResolvedValue([]);
    vi.spyOn(backendApi, 'externalTeams').mockResolvedValue([]);
    vi.spyOn(backendApi, 'externalTeamRoles').mockResolvedValue([]);

    document.body.innerHTML = '<div id="agents-page-root"></div>';
    await initAgentsPage();
    await loadAgentsPage();
    await loadAgentsPage();
    expect(backendApi.scanRuntimes).toHaveBeenCalledTimes(1);

    runtimes.mockResolvedValue([{
      id: 'runtime-codex',
      name: 'Codex',
      provider: 'codex',
      availability_status: 'ready',
    }]);
    await initAgentsPage();
    await loadAgentsPage();
    expect(backendApi.scanRuntimes).toHaveBeenCalledTimes(1);
  });

  it('Desktop Gateway 文本流按 Fast、AI 状态、final 顺序更新组队结果', async () => {
    let streamListener: ((event: {
      request_id: string;
      type: 'chunk' | 'end';
      text?: string;
    }) => void) | null = null;
    const snapshot = {
      leader_agent_id: 'crew::builtin',
      workflow: 'Leader 汇总。',
      members: [],
      requested_formation_mode: 'auto' as const,
      selected_formation_mode: 'fast' as const,
      fallback_reason: '',
      timing: { fast_ms: 1, ai_ms: 0, total_ms: 1 },
      warnings: [],
    };
    Object.defineProperty(window, 'Crew', {
      configurable: true,
      value: {
        gatewayStreamStart: async (requestId: string) => {
          await Promise.resolve();
          streamListener?.({
            request_id: requestId,
            type: 'chunk',
            text: `${JSON.stringify({ type: 'suggestion', phase: 'fast', suggestion: snapshot })}\n`,
          });
          streamListener?.({
            request_id: requestId,
            type: 'chunk',
            text: `${JSON.stringify({ type: 'status', phase: 'ai_reviewing' })}\n`,
          });
          streamListener?.({
            request_id: requestId,
            type: 'chunk',
            text: `${JSON.stringify({
              type: 'suggestion',
              phase: 'final',
              suggestion: { ...snapshot, selected_formation_mode: 'ai' },
            })}\n`,
          });
          streamListener?.({ request_id: requestId, type: 'end' });
          return { ok: true };
        },
        gatewayStreamCancel: vi.fn(async () => ({ ok: true })),
        onGatewayStreamEvent: (listener: typeof streamListener) => {
          streamListener = listener;
          return () => {
            streamListener = null;
          };
        },
      },
    });
    const phases: string[] = [];

    const result = await backendApi.suggestExternalTeamAuto(
      { name: '流式组队测试' },
      {
        onSuggestion: (_suggestion, phase) => phases.push(phase),
        onStatus: (phase) => phases.push(phase),
      },
    );

    expect(phases).toEqual(['fast', 'ai_reviewing', 'final']);
    expect(result.selected_formation_mode).toBe('ai');
    delete (window as Window & { Crew?: unknown }).Crew;
  });

  it('智能组队隐藏内部模式，并在 AI 检查和 Provider 失败回退时给出明确进度', async () => {
    configStore.set({
      config: {
        model: 'test',
        has_key: true,
        base_url: '',
        active_model_id: 'test',
        models: [],
        external_agents: { enabled: true },
      },
    });
    vi.spyOn(backendApi, 'runtimes').mockResolvedValue([]);
    vi.spyOn(backendApi, 'externalAgents').mockResolvedValue([]);
    vi.spyOn(backendApi, 'externalTeams').mockResolvedValue([]);
    vi.spyOn(backendApi, 'externalTeamRoles').mockResolvedValue([]);
    const fastSuggestion = {
      leader_agent_id: 'crew::builtin',
      workflow: 'Leader 拆解并汇总。',
      members: [{
        agent_id: 'crew::builtin',
        role: '负责拆解与汇总。',
        role_key: 'project_manager',
        role_label: '项目统筹',
        capabilities: ['planning'],
      }],
      requested_formation_mode: 'auto' as const,
      selected_formation_mode: 'fast' as const,
      fallback_reason: '',
      timing: { fast_ms: 2, ai_ms: 0, total_ms: 2 },
      warnings: [],
      formation_plan: {
        version: 1,
        leader_agent_id: 'crew::builtin',
        members: [{
          agent_id: 'crew::builtin',
          role_key: 'project_manager',
          role_label: '项目统筹',
          assigned_capabilities: ['planning'],
          responsibility: {},
          responsibility_markdown: '负责拆解与汇总。',
          selection_source: 'builtin',
          workflow_lane: 'lead',
          sort_order: 0,
        }],
        coverage: { required: ['planning'], covered: ['planning'], uncovered: [] },
        confidence: { requirement: 1, capability_evidence: 1, coverage: 1, overall: 1 },
        staffing_mode: 'minimal',
        excluded_agent_ids: [],
        reasons: [],
        warnings: [],
      },
    };
    let resolveAi!: (value: typeof fastSuggestion) => void;
    const aiSuggestion = new Promise<typeof fastSuggestion>((resolve) => {
      resolveAi = resolve;
    });
    vi.spyOn(backendApi, 'suggestExternalTeamAuto').mockImplementation(async (_payload, options) => {
      options?.onSuggestion?.(fastSuggestion, 'fast');
      options?.onStatus?.('ai_reviewing');
      const final = await aiSuggestion;
      options?.onSuggestion?.(final, 'final');
      return final;
    });

    document.body.innerHTML = '<div id="agents-page-root"></div>';
    await initAgentsPage();
    await loadAgentsPage();
    document.querySelector<HTMLElement>('[data-agents-tab="create-team"]')?.click();

    expect(document.querySelector('[data-formation-mode]')).toBeNull();
    expect(document.querySelector('[data-suggest-team]')?.textContent).toContain('智能组队');

    const nameInput = document.getElementById('team-name-input') as HTMLInputElement;
    nameInput.value = '测试团队';
    nameInput.dispatchEvent(new Event('input', { bubbles: true }));
    const descriptionInput = document.getElementById('team-description-input') as HTMLTextAreaElement;
    descriptionInput.value = '完成测试任务';
    descriptionInput.dispatchEvent(new Event('input', { bubbles: true }));
    document.querySelector<HTMLElement>('[data-suggest-team]')?.click();

    await vi.waitFor(() => {
      expect(document.querySelector('.formation-progress')?.textContent).toContain('智能检查优化');
      expect(document.querySelector('.formation-progress')?.textContent).toContain('正在智能组队');
      expect(document.querySelector('.formation-progress')?.classList.contains('formation-progress--ai_reviewing')).toBe(true);
      expect(document.querySelector('.formation-progress .team-mark')).toBeNull();
    });

    resolveAi({
      ...fastSuggestion,
      requested_formation_mode: 'auto',
      fallback_reason: 'provider_error',
      timing: { fast_ms: 2, ai_ms: 35_000, total_ms: 35_002 },
    });
    await vi.waitFor(() => {
      expect(document.querySelector('.formation-progress')?.textContent).toContain('智能检查暂未完成');
      expect(document.querySelector('.formation-progress')?.textContent).toMatch(/智能检查优化\s*·\s*\d+s/);
      expect(document.querySelector('[data-recheck-formation]')).not.toBeNull();
    });
    document.querySelector<HTMLElement>('[data-agents-tab="mine"]')?.click();
  });

  it('我的团队卡片可打开组队明细，操作按钮不会误触展开', async () => {
    configStore.set({
      config: {
        model: 'test',
        has_key: true,
        base_url: '',
        active_model_id: 'test',
        models: [],
        external_agents: { enabled: true },
      },
    });
    vi.spyOn(backendApi, 'runtimes').mockResolvedValue([]);
    vi.spyOn(backendApi, 'externalTeamRoles').mockResolvedValue([]);
    vi.spyOn(backendApi, 'externalAgents').mockResolvedValue([
      {
        id: 'agent-leader',
        name: 'Codex Leader',
        provider: 'codex',
        display_badge: 'X',
        runtime_id: 'runtime-codex',
        model: 'gpt-test',
      },
      {
        id: 'agent-reviewer',
        name: 'Claude Reviewer',
        provider: 'claude-code',
        display_badge: 'C',
        runtime_id: 'runtime-claude',
        model: 'sonnet',
      },
    ]);
    vi.spyOn(backendApi, 'externalTeams').mockResolvedValue([
      {
        id: 'team-product',
        name: '产品研发小队',
        description: '负责产品目标拆解与交付。',
        instructions: '1. Leader 拆解任务\n2. Reviewer 复核结果',
        leader_agent_id: 'agent-leader',
        members: [
          {
            id: 'member-leader',
            agent_id: 'agent-leader',
            agent_name: 'Codex Leader',
            display_badge: 'X',
            role: '### Leader 职责\n\n负责拆解与汇总。',
            role_label: '项目统筹',
            workflow_lane: 'lead',
            sort_order: 0,
          },
          {
            id: 'member-reviewer',
            agent_id: 'agent-reviewer',
            agent_name: 'Claude Reviewer',
            display_badge: 'C',
            role: '负责复核交付质量。',
            role_label: '质量复核',
            workflow_lane: 'review',
            sort_order: 1,
          },
        ],
      },
    ]);
    document.body.innerHTML = '<div id="agents-page-root"></div>';
    await initAgentsPage();
    await loadAgentsPage();

    const card = document.querySelector<HTMLElement>('[data-team-id="team-product"]');
    expect(card?.getAttribute('role')).toBe('button');
    card?.click();

    const modal = document.querySelector<HTMLElement>('[data-team-detail-backdrop]');
    expect(modal?.textContent).toContain('产品研发小队');
    expect(modal?.textContent).toContain('负责产品目标拆解与交付');
    expect(modal?.textContent).toContain('团队工作流');
    expect(modal?.textContent).toContain('Codex Leader');
    expect(modal?.textContent).toContain('Claude Reviewer');
    expect(modal?.textContent).toContain('负责复核交付质量');
    expect(modal?.querySelector('.team-modal-member.is-leader')).not.toBeNull();

    modal?.querySelector<HTMLElement>('[data-team-detail-close]')?.click();
    expect(document.querySelector('[data-team-detail-backdrop]')).toBeNull();

    vi.stubGlobal('confirm', vi.fn(() => false));
    document.querySelector<HTMLElement>('[data-delete-team="team-product"]')?.click();
    expect(document.querySelector('[data-team-detail-backdrop]')).toBeNull();
  });

  it('关闭时不展示入口且不请求目录，开启后按智能体和团队展示目录', async () => {
    configStore.set({
      config: {
        model: 'test',
        has_key: true,
        base_url: '',
        active_model_id: 'test',
        models: [],
        external_agents: { enabled: false },
      },
    });
    const agents = vi.spyOn(backendApi, 'externalAgents');
    const teams = vi.spyOn(backendApi, 'externalTeams');
    const runtimes = vi.spyOn(backendApi, 'runtimes');
    const ensureSession = vi.fn()
      .mockReturnValueOnce('new-agent-session')
      .mockReturnValueOnce('new-team-session');
    const assigned = vi.fn();
    const setAgentConfig = vi.spyOn(backendApi, 'setSessionAgentConfig').mockResolvedValue({
      ok: true,
    });
    vi.spyOn(backendApi, 'getSessionModel').mockResolvedValue({
      model_profile_id: 'gpt-test',
      model_label: 'GPT Test',
      pending_model_profile_id: null,
      pending_label: null,
      has_pending: false,
      pending: false,
      source: 'external',
      models: [],
      model_switchable: true,
    });
    await initAgentsPage({
      ensureChatSession: ensureSession,
      onSessionAgentAssigned: assigned,
    });
    mountComposer();

    document.getElementById('chat-craft-btn')?.click();
    const entry = document.querySelector<HTMLElement>('[data-craft-mode="external"]');
    expect(entry).toBeNull();
    expect(document.getElementById('chat-external-inline-popover')).toBeNull();
    expect(agents).not.toHaveBeenCalled();
    expect(teams).not.toHaveBeenCalled();
    expect(runtimes).not.toHaveBeenCalled();

    configStore.set({
      config: {
        model: 'test',
        has_key: true,
        base_url: '',
        active_model_id: 'test',
        models: [],
        external_agents: { enabled: true },
      },
    });
    runtimes.mockResolvedValue([
      {
        id: 'runtime-codex',
        name: 'Codex',
        provider: 'codex',
        display_badge: 'X',
        availability_status: 'ready',
      },
      {
        id: 'runtime-kimi',
        name: 'Kimi',
        provider: 'kimi',
        display_badge: 'K',
        availability_status: 'ready',
      },
      {
        id: 'runtime-hermes',
        name: 'Hermes',
        provider: 'hermes',
        display_badge: 'H',
        availability_status: 'ready',
      },
      {
        id: 'runtime-claude',
        name: 'Claude Code',
        provider: 'claude-code',
        display_badge: 'C',
        availability_status: 'ready',
      },
    ]);
    agents.mockResolvedValue([
      {
        id: 'agent-codex',
        name: 'Codex 开发助手',
        provider: 'codex',
        display_badge: 'X',
        runtime_id: 'runtime-codex',
        model: 'gpt-test',
        status: 'ready',
      },
      {
        id: 'agent-kimi',
        name: '小助手',
        provider: 'kimi',
        display_badge: 'K',
        runtime_id: 'runtime-kimi',
        status: 'ready',
      },
      {
        id: 'agent-hermes',
        name: '评审助手',
        provider: 'hermes',
        display_badge: 'H',
        runtime_id: 'runtime-hermes',
        status: 'ready',
      },
      {
        id: 'agent-claude',
        name: '代码助手',
        provider: 'claude-code',
        display_badge: 'C',
        runtime_id: 'runtime-claude',
        status: 'ready',
      },
      {
        id: 'agent-contract-error',
        name: '不应前端推断',
        provider: 'codex',
        runtime_id: 'runtime-codex',
        status: 'ready',
      },
    ]);
    teams.mockResolvedValue([
      {
        id: 'team-dev',
        name: '研发外援团',
        display_badge: 'T',
        description: '这是一个不应在快捷选择器中展示的冗长团队目标说明',
        leader_agent_id: 'agent-codex',
        members: [{ agent_id: 'agent-codex' }],
      },
    ]);
    vi.stubGlobal('innerHeight', 800);
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue({
      x: 20,
      y: 700,
      top: 700,
      right: 120,
      bottom: 732,
      left: 20,
      width: 100,
      height: 32,
      toJSON: () => ({}),
    });
    vi.spyOn(HTMLElement.prototype, 'offsetHeight', 'get').mockImplementation(function () {
      if (this.id !== 'chat-external-inline-popover') return 0;
      return this.textContent?.includes('正在加载外援') ? 80 : 360;
    });

    // 关闭旧菜单，再按开启后的配置重新打开。
    document.getElementById('chat-craft-btn')?.click();
    await openExternalEntry();

    const popover = document.getElementById('chat-external-inline-popover');
    expect(popover?.textContent).toContain('外援');
    expect(popover?.textContent).toContain('Codex 开发助手');
    expect(popover?.textContent).toContain('外援团队');
    expect(popover?.textContent).toContain('研发外援团');
    expect(popover?.textContent).toContain('1 名成员 · Codex 开发助手 Leader');
    expect(popover?.textContent).not.toContain('冗长团队目标说明');
    expect(
      popover?.querySelector('[data-external-agent-id="agent-codex"] .composer-agent-pixel-icon')
        ?.textContent,
    ).toBe('X');
    expect(
      popover?.querySelector('[data-external-agent-id="agent-kimi"] .composer-agent-pixel-icon')
        ?.textContent,
    ).toBe('K');
    expect(
      popover?.querySelector('[data-external-agent-id="agent-hermes"] .composer-agent-pixel-icon')
        ?.textContent,
    ).toBe('H');
    expect(
      popover?.querySelector('[data-external-agent-id="agent-claude"] .composer-agent-pixel-icon')
        ?.textContent,
    ).toBe('C');
    expect(
      popover?.querySelector('[data-external-agent-id="agent-contract-error"] .composer-agent-pixel-icon')
        ?.textContent,
    ).toBe('?');
    expect(
      popover
        ?.querySelector('[data-external-agent-id="agent-codex"] .composer-agent-pixel-icon')
        ?.className,
    ).toMatch(/composer-agent-pixel-icon--tone-\d/);
    expect(
      popover?.querySelector(
        '[data-external-team-id="team-dev"] .composer-agent-pixel-icon',
      ),
    ).toBeNull();
    expect(
      popover?.querySelectorAll(
        '[data-external-team-id="team-dev"] .composer-agent-team-logo .session__team-logo i',
      ),
    ).toHaveLength(2);
    await vi.waitFor(() => expect(popover?.style.top).toBe('334px'));

    popover?.querySelector<HTMLElement>('[data-external-agent-id="agent-codex"]')?.click();
    await vi.waitFor(() => expect(setAgentConfig).toHaveBeenCalledOnce());
    await vi.waitFor(() => expect(assigned).toHaveBeenCalledOnce());
    expect(ensureSession).toHaveBeenCalledOnce();
    expect(setAgentConfig).toHaveBeenCalledWith('new-agent-session', {
      executor: 'external',
      external_agent_id: 'agent-codex',
      external: { external_agent_id: 'agent-codex' },
    });
    expect(assigned).toHaveBeenCalledWith(
      'new-agent-session',
      {
        name: 'Codex 开发助手',
        provider: 'codex',
        display_badge: 'X',
        model: 'gpt-test',
      },
      'gpt-test',
      { kind: 'external_agent', id: 'agent-codex' },
    );

    await openExternalEntry();
    document
      .getElementById('chat-external-inline-popover')
      ?.querySelector<HTMLElement>('[data-external-team-id="team-dev"]')
      ?.click();
    await vi.waitFor(() => expect(setAgentConfig).toHaveBeenCalledTimes(2));
    await vi.waitFor(() => expect(assigned).toHaveBeenCalledTimes(2));
    expect(setAgentConfig).toHaveBeenLastCalledWith('new-team-session', {
      executor: 'team',
      team: { external_team_id: 'team-dev' },
    });
    expect(assigned).toHaveBeenLastCalledWith(
      'new-team-session',
      { name: '研发外援团', provider: 'team', display_badge: 'T' },
      '',
      { kind: 'external_team', id: 'team-dev' },
    );
    expect(ensureSession).toHaveBeenCalledTimes(2);
  });
});
