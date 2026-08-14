/**
 * Team Session「协作」看板测试。
 *
 * 覆盖 Desktop TypeScript DOM 对 Web TaskBoard 的核心数据与 UI 契约：
 * DAG 分层、状态归一、团队成员、节点摘要、产物、执行日志和运行态。
 *
 * @vitest-environment happy-dom
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { backendApi, type Task } from '../../src/ui/backend-client';
import {
  __resetTeamCollaborationBoardForTest,
  activateTeamCollaborationBoard,
  buildTeamCollaborationBoardHtml,
  makeTeamFlowNodes,
  makeTeamFlowTurns,
  normalizeTeamFlowStatus,
  primeTeamCollaborationIdentity,
  refreshTeamCollaborationBoard,
  resolveTeamCollaborationMember,
  resolveTeamCollaborationName,
} from '../../src/ui/features/team-collaboration-board';
import { __resetAllStoresForTest, messageStore } from '../../src/ui/stores/stores';

const SESSION_ID = 'team-session-board';

const tasks: Task[] = [
  {
    id: 'leader_plan',
    task_id: 'leader_plan',
    kind: 'team',
    session_id: SESSION_ID,
    title: '分析需求并拆分任务',
    assignee: 'leader',
    status: 'completed',
    result: '结论：已拆分任务\n依据：形成三层 DAG',
    output_ref: '',
    created_at: 1,
    finished_at: 2,
    progress: {
      source: 'team_plan',
      plan_node_id: 'leader_plan',
      display_order: 10,
      workflow_lane: 'lead',
      turn_session_id: SESSION_ID,
      turn_title: '实现协作看板',
      summary_items: ['结论：已拆分任务', '依据：形成三层 DAG'],
    },
  },
  {
    id: 'build',
    task_id: 'build',
    kind: 'team',
    session_id: SESSION_ID,
    title: '开发桌面协作看板',
    assignee: 'agent-frontend',
    status: 'running',
    result: '',
    output_ref: '/tmp/team-board.ts',
    created_at: 3,
    progress: {
      source: 'team_plan',
      plan_node_id: 'build',
      parent_node_ids: ['leader_plan'],
      display_order: 40,
      workflow_lane: 'build',
      role_label: '前端开发',
      turn_session_id: SESSION_ID,
      execution_events: [{ kind: 'tool', event_title: '工具调用：apply_patch', event_text: '更新 Desktop DOM' }],
    },
  },
  {
    id: 'leader_summary',
    task_id: 'leader_summary',
    kind: 'team',
    session_id: SESSION_ID,
    title: '汇总交付结果',
    assignee: 'leader',
    status: 'pending',
    result: '',
    output_ref: '',
    created_at: 4,
    progress: {
      source: 'team_plan',
      plan_node_id: 'leader_summary',
      parent_node_ids: ['build'],
      display_order: 80,
      workflow_lane: 'summary',
      turn_session_id: SESSION_ID,
    },
  },
];

beforeEach(() => {
  __resetAllStoresForTest();
  __resetTeamCollaborationBoardForTest();
  messageStore.set({ messages: { [SESSION_ID]: [] } });
  vi.spyOn(backendApi, 'tasks').mockResolvedValue(tasks);
  vi.spyOn(backendApi, 'runtimeConcurrency').mockResolvedValue({
    max_active_runs: 4,
    global_active: 1,
    global_queued: 2,
    sessions: { [SESSION_ID]: { live: 'running', queue_depth: 0 } },
    active_children: [{ task_id: 'build' }],
  });
  vi.spyOn(backendApi, 'getSessionAgentConfig').mockResolvedValue({
    team: { external_team_id: 'team-product' },
  });
  vi.spyOn(backendApi, 'externalTeams').mockResolvedValue([{
    id: 'team-product',
    name: '产品研发团队',
    leader_agent_id: 'crew::builtin',
    members: [
      {
        agent_id: 'crew::builtin',
        role: '## 职责\n项目计划与验收把关\n## 工作原则\n先确认目标、输入、输出和验收标准，再执行。',
        sort_order: 0,
      },
      {
        agent_id: 'agent-frontend',
        agent_name: '前端工程师',
        role: '## 职责\n负责桌面端实现\n## 工作原则\n优先小步交付可验证结果。',
        sort_order: 1,
      },
    ],
  }]);
  vi.spyOn(backendApi, 'externalAgents').mockResolvedValue([]);
});

afterEach(() => {
  __resetTeamCollaborationBoardForTest();
  vi.restoreAllMocks();
});

describe('Team Flow 数据投影', () => {
  it('归一后端状态并按依赖生成三层 DAG', () => {
    expect(normalizeTeamFlowStatus({ ...tasks[0], status: 'success' })).toBe('completed');
    expect(normalizeTeamFlowStatus({ ...tasks[0], status: 'waiting_input' })).toBe('blocked');

    const nodes = makeTeamFlowNodes(tasks);
    const turns = makeTeamFlowTurns(nodes);

    expect(nodes.map((node) => node.title)).toEqual(['分析需求并拆分任务', '开发桌面协作看板', '汇总交付结果']);
    expect(turns).toHaveLength(1);
    expect(turns[0]?.stages.map((stage) => stage.nodes.map((node) => node.id))).toEqual([
      ['leader_plan'],
      ['build'],
      ['leader_summary'],
    ]);
    expect(turns[0]?.status).toBe('running');
  });
});

describe('协作看板 HTML', () => {
  it('首发前可只预热团队名称和真实 Leader 身份', async () => {
    await primeTeamCollaborationIdentity(SESSION_ID);

    expect(resolveTeamCollaborationName(SESSION_ID)).toBe('产品研发团队');
    expect(resolveTeamCollaborationMember(SESSION_ID, {
      id: 'leader-live',
      role: 'team_internal',
      content: '',
      timestamp: 1,
      agentId: 'leader',
      agentName: 'leader',
      isLeader: true,
      streaming: true,
    })).toMatchObject({
      agentId: 'crew::builtin',
      name: 'Crew',
      isLeader: true,
    });
    expect(backendApi.tasks).not.toHaveBeenCalled();
    expect(backendApi.runtimeConcurrency).not.toHaveBeenCalled();
  });

  it('完整呈现 Web TaskBoard 的核心区域和 Team 配置成员', async () => {
    await refreshTeamCollaborationBoard(SESSION_ID);

    const html = buildTeamCollaborationBoardHtml(SESSION_ID);

    expect(html).toContain('协作看板');
    expect(html).toContain('Team Flow');
    expect(html).toContain('团队 DAG 工作流');
    expect(html).toContain('Crew');
    expect(html).toContain('href="#avatar-headphones"');
    expect(html).not.toContain('assistant.png');
    expect(html).not.toContain('Crew 内置智能体');
    expect(html).toContain('Leader 统筹团队协作，负责项目计划与验收把关');
    expect(html).toContain('前端工程师');
    expect(html).toContain('负责桌面端实现');
    expect(html).not.toContain('工作原则');
    expect(html).not.toContain('先确认目标');
    expect(html).toContain('分析需求并拆分任务');
    expect(html).toContain('开发桌面协作看板');
    expect(html).toContain('依赖：分析需求并拆分任务');
    expect(html).toContain('运行态');
    expect(html).toContain('全局排队');
    expect(html).toContain('活跃子任务');
    expect(html).not.toContain('React');

    document.body.innerHTML = html;
    activateTeamCollaborationBoard(SESSION_ID);
    document.querySelector<HTMLButtonElement>('[data-team-files]')?.click();
    document.querySelector<HTMLButtonElement>('[data-team-node="build"]')?.click();
    const expandedHtml = buildTeamCollaborationBoardHtml(SESSION_ID);
    expect(expandedHtml).toContain('产物文件');
    expect(expandedHtml).toContain('data-team-open-path="/tmp/team-board.ts"');
    expect(expandedHtml).toContain('执行详情');
    expect(expandedHtml).toContain('工具调用：apply_patch');
  });

  it('轮询数据未变化时不触发看板重绘，避免中断滚动条拖拽', async () => {
    await refreshTeamCollaborationBoard(SESSION_ID);
    let updates = 0;
    const listener = (): void => { updates += 1; };
    window.addEventListener('team-collaboration:updated', listener);

    await refreshTeamCollaborationBoard(SESSION_ID);

    window.removeEventListener('team-collaboration:updated', listener);
    expect(updates).toBe(0);
  });

  it('无节点时仍显示与 Web 一致的可爱空态和运行态', async () => {
    vi.mocked(backendApi.tasks).mockResolvedValueOnce([]);
    vi.mocked(backendApi.runtimeConcurrency).mockResolvedValueOnce({
      max_active_runs: 4,
      global_active: 0,
      global_queued: 0,
      sessions: {},
      active_children: [],
    });
    await refreshTeamCollaborationBoard(SESSION_ID);

    const html = buildTeamCollaborationBoardHtml(SESSION_ID);

    expect(html).toContain('还没有流程节点');
    expect(html).toContain('pixel-empty');
    expect(html).toContain('暂无运行或排队会话');
  });
});
