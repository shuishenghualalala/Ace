/**
 * @vitest-environment happy-dom
 *
 * agents-page 单测：页面生命周期（激活/停用/能力门控/晚到响应守卫）。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { backendApi } from '../../src/ui/backend-client';
import { createAgentsPageContribution, initAgentsPage, loadAgentsPage } from '../../src/ui/features/agents-page';
import { __resetAllStoresForTest, configStore } from '../../src/ui/stores/stores';

const mockShowConfirmDialog = vi.hoisted(() => vi.fn(async () => true));

vi.mock('../../src/ui/backend-client', () => ({
  backendApi: {
    runtimes: vi.fn(),
    externalAgents: vi.fn(),
    externalTeams: vi.fn(),
    externalTeamRoles: vi.fn(),
    scanRuntimes: vi.fn(),
    createExternalAgent: vi.fn(),
    createExternalTeam: vi.fn(),
    deleteExternalAgent: vi.fn(),
    deleteExternalTeam: vi.fn(),
    deleteRuntime: vi.fn(),
    suggestExternalTeamAuto: vi.fn(),
    draftExternalTeamDescription: vi.fn(),
  },
}));

vi.mock('../../src/ui/ui-feedback', () => ({
  showConfirmDialog: mockShowConfirmDialog,
  showPromptDialog: vi.fn(async () => null),
}));

const api = backendApi as unknown as {
  runtimes: ReturnType<typeof vi.fn>;
  externalAgents: ReturnType<typeof vi.fn>;
  externalTeams: ReturnType<typeof vi.fn>;
  externalTeamRoles: ReturnType<typeof vi.fn>;
  scanRuntimes: ReturnType<typeof vi.fn>;
};

const flush = () => new Promise((r) => setTimeout(r, 0));

function enableExternalAgents(): void {
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
}

function disableExternalAgents(): void {
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
}

beforeEach(() => {
  vi.clearAllMocks();
  mockShowConfirmDialog.mockResolvedValue(true);
  __resetAllStoresForTest();
  enableExternalAgents();
  document.body.innerHTML = `
    <button class="nav-item" data-tab="agents">Agents</button>
    <section id="agents-tab" class="tab-pane"><div id="agents-page-root"></div></section>
  `;
  api.runtimes.mockResolvedValue([]);
  api.externalAgents.mockResolvedValue([]);
  api.externalTeams.mockResolvedValue([]);
  api.externalTeamRoles.mockResolvedValue([
    {
      key: 'project_manager',
      label: '项目统筹',
      description: '负责拆解目标、分配任务、检查结果并汇总交付。',
      capabilities: ['planning', 'review', 'synthesis'],
      workflow_lane: 'lead',
    },
  ]);
  api.scanRuntimes.mockResolvedValue([]);
});

describe('Agents 页面生命周期', () => {
  it('activate 加载并渲染外援页面', async () => {
    const contribution = createAgentsPageContribution();
    const controller = new AbortController();

    contribution.activate({ signal: controller.signal });
    await flush();

    const root = document.querySelector('#agents-page-root');
    expect(root).not.toBeNull();
    expect(root?.textContent).toContain('外援');
    expect(api.externalAgents).toHaveBeenCalled();
  });

  it('重复 activate 不累积全局事件监听器', async () => {
    const contribution = createAgentsPageContribution();
    const addEventListenerSpy = vi.spyOn(document, 'addEventListener');

    contribution.activate({ signal: new AbortController().signal });
    const firstCount = addEventListenerSpy.mock.calls.filter(([type]) =>
      type === 'mousedown' || type === 'keydown' || type === 'click',
    ).length;

    contribution.deactivate();
    contribution.activate({ signal: new AbortController().signal });
    const secondCount = addEventListenerSpy.mock.calls.filter(([type]) =>
      type === 'mousedown' || type === 'keydown' || type === 'click',
    ).length;

    // 每次 activate 只绑定一组（mousedown/keydown/click），旧的一组已被解绑。
    expect(secondCount - firstCount).toBe(3);
    addEventListenerSpy.mockRestore();
  });

  it('deactivate 释放页面资源', async () => {
    const contribution = createAgentsPageContribution();
    const controller = new AbortController();

    contribution.activate({ signal: controller.signal });
    await flush();

    await contribution.deactivate();

    // 停用后页面根节点被清空（disposeAgentsPage 释放 view）。
    expect(document.querySelector('#agents-page-root')?.children.length).toBe(0);
  });

  it('禁用时 isAvailable 返回 false', () => {
    disableExternalAgents();
    const contribution = createAgentsPageContribution();
    expect(contribution.isAvailable?.()).toBe(false);
  });

  it('离页后旧加载响应不回写页面', async () => {
    let resolveAgents!: (value: unknown[]) => void;
    api.externalAgents.mockReturnValue(new Promise((resolve) => { resolveAgents = resolve; }));

    const contribution = createAgentsPageContribution();
    const controller = new AbortController();
    contribution.activate({ signal: controller.signal });
    await flush();

    controller.abort();
    await contribution.deactivate();
    resolveAgents([{ id: 'late', name: 'Late Agent', provider: 'test', model: 'm', runtime_id: '', system_prompt: '', custom_args: [], custom_env: {} }]);
    await flush();

    // 晚到响应不应渲染“Late Agent”
    expect(document.querySelector('#agents-page-root')?.textContent).not.toContain('Late Agent');
  });

  it('loadAgentsPage 在禁用时直接返回', async () => {
    disableExternalAgents();
    await loadAgentsPage();
    expect(api.externalAgents).not.toHaveBeenCalled();
  });
});

describe('initAgentsPage 回调', () => {
  it('接收 ensureChatSession 与 onSessionAgentAssigned 回调', async () => {
    const ensureChatSession = vi.fn(() => 'session-1');
    const onSessionAgentAssigned = vi.fn();
    await initAgentsPage({ ensureChatSession, onSessionAgentAssigned });
    expect(() => initAgentsPage({ ensureChatSession, onSessionAgentAssigned })).not.toThrow();
  });
});
