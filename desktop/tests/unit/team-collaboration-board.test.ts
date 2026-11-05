/**
 * Team Session「协作」看板测试。
 *
 * 覆盖 Desktop TypeScript DOM 对 Web TaskBoard 的核心数据与 UI 契约：
 * DAG 分层、状态归一、团队成员、节点摘要、产物、消息定位和运行态。
 *
 * @vitest-environment happy-dom
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { backendApi, type Task } from '../../src/ui/backend-client';
import {
  __resetTeamCollaborationBoardForTest,
  activateTeamCollaborationBoard,
  buildTeamCollaborationBoardHtml,
  disposeTeamCollaborationBoard,
  initTeamCollaborationBoard,
  makeTeamFlowNodes,
  makeTeamFlowTurns,
  normalizeTeamFlowStatus,
  nodeMessageId,
  primeTeamCollaborationIdentity,
  refreshTeamCollaborationBoard,
  resolveTeamCollaborationMember,
  resolveTeamCollaborationName,
  teamCollaborationTaskCount,
} from '../../src/ui/features/team-collaboration-board';
import { __resetAllStoresForTest, messageStore } from '../../src/ui/stores/stores';
import { setActiveSessionId } from '../../src/ui/state';
import { featureEventRegistry } from '../../src/ui/features/event-reducer-registry';
import * as boardHooks from '../../src/ui/features/board-hooks';

const SESSION_ID = 'team-session-board';

type SessionAgentConfigResponse = Awaited<ReturnType<typeof backendApi.getSessionAgentConfig>>;

/** 手工控制 resolve/reject 时机与结果的 Promise，用于构造「在途迟到」场景。 */
function deferred<T>(): {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (reason?: unknown) => void;
} {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

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

describe('blocked Team node ownership', () => {
  it('shows an unassigned blocked node as waiting for assignment', () => {
    const nodes = makeTeamFlowNodes([{
      id: 'blocked-verify',
      task_id: 'blocked-verify',
      kind: 'team',
      session_id: SESSION_ID,
      title: '测试验证',
      assignee: '',
      status: 'blocked',
      result: '用户拒绝补员',
      progress: {
        source: 'team_kanban',
        plan_node_id: 'verify',
        runtime_blocking: { status: 'blocked' },
        previous_assignee: 'kk',
      },
    }]);

    expect(nodes[0].owner).toBe('待分配');
  });
});

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
  vi.spyOn(backendApi, 'getSessionModel').mockResolvedValue({
    ok: true,
    source: 'team',
    scope: 'team',
    external_team_id: 'team-product',
    model_binding_revision: 1,
    members: [
      {
        member_id: 'crew::builtin',
        member_name: 'Crew',
        is_leader: true,
        model_profile_id: 'crew-model',
        model_label: 'Crew Model',
        model_switchable: true,
        status: 'idle',
        models: [{ id: 'crew-model', label: 'Crew Model', default: true }, { id: 'crew-fast', label: 'Crew Fast' }],
      },
      {
        member_id: 'agent-frontend',
        member_name: '前端工程师',
        is_leader: false,
        model_profile_id: 'frontend-model',
        model_label: 'Frontend Model',
        model_switchable: true,
        status: 'idle',
        models: [{ id: 'frontend-model', label: 'Frontend Model', default: true }, { id: 'frontend-fast', label: 'Frontend Fast' }],
      },
    ],
  });
  vi.spyOn(backendApi, 'setSessionModel').mockResolvedValue({
    ok: true,
    source: 'team',
    scope: 'team_member',
    member_id: 'agent-frontend',
    member_name: '前端工程师',
    model_profile_id: 'frontend-fast',
    model_label: 'Frontend Fast',
  });
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
    expect(html).toContain('团队工作流');
    expect(html).toContain('Crew');
    expect(html).toContain('href="#avatar-headphones"');
    expect(html).toContain('class="pixel-flag"');
    expect(html).not.toContain('class="leader-flag"');
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
    expect(expandedHtml).not.toContain('执行详情');
    expect(expandedHtml).not.toContain('工具调用：apply_patch');
  });

  it('为节点生成对应团队消息的定位目标', () => {
    const node = makeTeamFlowNodes([tasks[1]])[0];
    expect(nodeMessageId(node, [
      { id: 'team-build', role: 'team_internal', content: '已完成', timestamp: 1, nodeId: 'build' },
    ])).toBe('team-build');
  });

  it('在成员卡片提供按成员切换模型的入口', async () => {
    await refreshTeamCollaborationBoard(SESSION_ID);
    document.body.innerHTML = buildTeamCollaborationBoardHtml(SESSION_ID);
    activateTeamCollaborationBoard(SESSION_ID);

    const selector = document.querySelector<HTMLSelectElement>('[data-team-member-id="agent-frontend"]');
    expect(selector).not.toBeNull();
    expect(selector?.value).toBe('frontend-model');
    expect(selector?.options).toHaveLength(2);

    if (!selector) throw new Error('成员模型选择器缺失');
    selector.value = 'frontend-fast';
    selector.dispatchEvent(new Event('change'));
    expect(backendApi.setSessionModel).toHaveBeenCalledWith(
      SESSION_ID,
      'frontend-fast',
      { member_id: 'agent-frontend' },
    );
  });

  it('不把后台 agent_turn 的 task JSON 当成用户产物', async () => {
    vi.mocked(backendApi.tasks).mockResolvedValueOnce([{
      ...tasks[0],
      output_ref: '/owner/.crew/tasks/task_internal.json',
    }]);
    await refreshTeamCollaborationBoard(SESSION_ID);

    const html = buildTeamCollaborationBoardHtml(SESSION_ID);

    expect(html).not.toContain('task_internal.json');
    expect(html).not.toContain('产物');
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

describe('卸载/切会话后在途请求守卫', () => {
  it('卸载后迟到的刷新响应不得复活缓存或派发更新事件', async () => {
    initTeamCollaborationBoard();
    const deferredTasks = deferred<Task[]>();
    vi.mocked(backendApi.tasks).mockReturnValueOnce(deferredTasks.promise);
    let updates = 0;
    const listener = (): void => { updates += 1; };
    window.addEventListener('team-collaboration:updated', listener);

    const refreshing = refreshTeamCollaborationBoard(SESSION_ID);
    disposeTeamCollaborationBoard();
    deferredTasks.resolve(tasks);
    await refreshing;

    window.removeEventListener('team-collaboration:updated', listener);
    // 卸载后迟到响应不得把已清空的快照缓存"复活"
    expect(teamCollaborationTaskCount(SESSION_ID)).toBe(0);
    expect(updates).toBe(0);
  });

  it('dispose → init 后上一代刷新失效，新一代刷新正常', async () => {
    initTeamCollaborationBoard();
    const deferredTasks = deferred<Task[]>();
    vi.mocked(backendApi.tasks).mockReturnValueOnce(deferredTasks.promise);
    let updates = 0;
    const listener = (): void => { updates += 1; };
    window.addEventListener('team-collaboration:updated', listener);

    const stale = refreshTeamCollaborationBoard(SESSION_ID);
    disposeTeamCollaborationBoard();
    initTeamCollaborationBoard();
    deferredTasks.resolve(tasks);
    await stale;
    expect(teamCollaborationTaskCount(SESSION_ID)).toBe(0);

    // 新一代刷新正常回写并恰好派发一次更新事件
    await refreshTeamCollaborationBoard(SESSION_ID);
    window.removeEventListener('team-collaboration:updated', listener);
    expect(teamCollaborationTaskCount(SESSION_ID)).toBe(tasks.length);
    expect(updates).toBe(1);
  });

  it('卸载后迟到的身份预热不得写入快照', async () => {
    initTeamCollaborationBoard();
    const deferredConfig = deferred<SessionAgentConfigResponse>();
    vi.mocked(backendApi.getSessionAgentConfig).mockReturnValueOnce(deferredConfig.promise);

    const priming = primeTeamCollaborationIdentity(SESSION_ID);
    disposeTeamCollaborationBoard();
    deferredConfig.resolve({ team: { external_team_id: 'team-product' } });
    await priming;

    expect(resolveTeamCollaborationName(SESSION_ID)).toBeUndefined();
  });

  it('切换会话后迟到响应不污染新会话看板（按会话键控隔离）', async () => {
    initTeamCollaborationBoard();
    setActiveSessionId('session-b');
    const deferredTasks = deferred<Task[]>();
    vi.mocked(backendApi.tasks).mockReturnValueOnce(deferredTasks.promise);

    const refreshing = refreshTeamCollaborationBoard(SESSION_ID);
    deferredTasks.resolve(tasks);
    await refreshing;

    // 新会话视图不被旧会话的迟到响应污染
    expect(teamCollaborationTaskCount('session-b')).toBe(0);
    expect(resolveTeamCollaborationName('session-b')).toBeUndefined();
    // 旧会话自身缓存按键控正常更新（切回时数据可用）
    expect(teamCollaborationTaskCount(SESSION_ID)).toBe(tasks.length);
  });
});

describe('刷新占用的 token 所有权（旧代不得释放新代占用）', () => {
  it('旧代刷新结束后再次发起刷新：不得绕过新代请求的占用标记', async () => {
    initTeamCollaborationBoard();
    const staleTasks = deferred<Task[]>();
    vi.mocked(backendApi.tasks).mockReturnValueOnce(staleTasks.promise); // 第 1 次：旧代挂起
    let updates = 0;
    const listener = (): void => { updates += 1; };
    window.addEventListener('team-collaboration:updated', listener);

    const stale = refreshTeamCollaborationBoard(SESSION_ID);
    disposeTeamCollaborationBoard();
    initTeamCollaborationBoard();

    const freshTasks = deferred<Task[]>();
    vi.mocked(backendApi.tasks).mockReturnValueOnce(freshTasks.promise); // 第 2 次：新代挂起
    const fresh = refreshTeamCollaborationBoard(SESSION_ID);

    staleTasks.resolve([{ ...tasks[0], id: 'old-generation-only', task_id: 'old-generation-only' }]);
    await stale;

    // 旧代结束后的再次刷新必须被新代占用标记挡住：不并发、不提前回写
    const thirdTasks = deferred<Task[]>();
    vi.mocked(backendApi.tasks).mockReturnValueOnce(thirdTasks.promise); // 若被绕过会发出第 3 次
    const third = refreshTeamCollaborationBoard(SESSION_ID);
    thirdTasks.resolve([{ ...tasks[0], id: 'stale-third', task_id: 'stale-third' }]);
    await third;

    expect(backendApi.tasks).toHaveBeenCalledTimes(2);
    expect(teamCollaborationTaskCount(SESSION_ID)).toBe(0); // 新代请求仍在途：无乱序回写

    freshTasks.resolve(tasks);
    await fresh;
    expect(teamCollaborationTaskCount(SESSION_ID)).toBe(tasks.length);
    expect(updates).toBe(1);

    window.removeEventListener('team-collaboration:updated', listener);
  });

  it('旧代请求失败（reject）也不得清除新代请求的占用标记，占有者结束后正常释放', async () => {
    initTeamCollaborationBoard();
    const staleTasks = deferred<Task[]>();
    vi.mocked(backendApi.tasks).mockReturnValueOnce(staleTasks.promise);
    const stale = refreshTeamCollaborationBoard(SESSION_ID);
    disposeTeamCollaborationBoard();
    initTeamCollaborationBoard();

    const freshTasks = deferred<Task[]>();
    vi.mocked(backendApi.tasks).mockReturnValueOnce(freshTasks.promise);
    const fresh = refreshTeamCollaborationBoard(SESSION_ID);

    staleTasks.reject(new Error('旧代请求失败'));
    await stale; // tasks 的 .catch 兜底 → 代际守卫丢弃 → finally 不释放新代 token

    // 占用标记仍在：并发刷新被挡住，不发第三次请求
    await refreshTeamCollaborationBoard(SESSION_ID);
    expect(backendApi.tasks).toHaveBeenCalledTimes(2);

    freshTasks.resolve(tasks);
    await fresh;
    expect(teamCollaborationTaskCount(SESSION_ID)).toBe(tasks.length);

    // 占有者结束后正常释放：后续刷新可发起新请求
    await refreshTeamCollaborationBoard(SESSION_ID);
    expect(backendApi.tasks).toHaveBeenCalledTimes(3);
  });

  it('切会话后占用按会话键控隔离，互不阻塞', async () => {
    initTeamCollaborationBoard();
    const deferredA = deferred<Task[]>();
    vi.mocked(backendApi.tasks).mockReturnValueOnce(deferredA.promise);
    const pendingA = refreshTeamCollaborationBoard('session-a');

    // B 会话不受 A 会话在途占用的影响
    await refreshTeamCollaborationBoard('session-b');
    expect(teamCollaborationTaskCount('session-b')).toBe(tasks.length);

    deferredA.resolve(tasks);
    await pendingA;
    expect(teamCollaborationTaskCount('session-a')).toBe(tasks.length);
  });
});

describe('team-collaboration-board 安装事务回滚（单 Feature 事务）', () => {
  const dummyCtx = {
    sessionId: 's1',
    messages: [],
    book: {
      assistantId: null,
      firstChunkAt: null,
      activeRequestId: null,
      turnSealed: false,
      acceptingNewRequest: true,
      hadTeamInternal: false,
    },
    now: Date.now(),
    sequence: 0,
  };

  function dispatchInternalMessage(): ReturnType<typeof featureEventRegistry.dispatch> {
    return featureEventRegistry.dispatch('team', 'internal_message', 1, { text: 'hello' }, dummyCtx);
  }

  /** registry 未命中会 console.warn 噪声，断言「无残留」时抑制。 */
  function silenceUnhandledWarns(): () => void {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    return () => warn.mockRestore();
  }

  it('reducer 注册失败：无残留、解除故障后重试成功、重复 dispose 幂等', () => {
    const original = featureEventRegistry.register.bind(featureEventRegistry);
    const spy = vi.spyOn(featureEventRegistry, 'register').mockImplementation((reg) => {
      if (reg.feature === 'team' && reg.event === 'internal_message') {
        throw new Error('注入：team reducer 注册失败');
      }
      return original(reg);
    });

    expect(() => initTeamCollaborationBoard()).toThrow('注入：team reducer 注册失败');

    const restoreWarn = silenceUnhandledWarns();
    try {
      expect(dispatchInternalMessage()).toBeNull();
    } finally {
      restoreWarn();
    }

    spy.mockRestore();
    const disposer = initTeamCollaborationBoard();
    expect(dispatchInternalMessage()).not.toBeNull();

    disposer();
    disposer();
    const restoreWarn2 = silenceUnhandledWarns();
    try {
      expect(dispatchInternalMessage()).toBeNull();
    } finally {
      restoreWarn2();
    }
  });

  it('hooks 注册失败：已注册的 reducer 逆序回滚，解除故障后重试成功', () => {
    vi.spyOn(boardHooks, 'registerTeamBoardCallbacks').mockImplementationOnce(() => {
      throw new Error('注入：team hooks 注册失败');
    });

    expect(() => initTeamCollaborationBoard()).toThrow('注入：team hooks 注册失败');

    // 旧实现此处 reducer 残留（dispatch 非 null）且无清理路径，重试会撞 already registered
    const restoreWarn = silenceUnhandledWarns();
    try {
      expect(dispatchInternalMessage()).toBeNull();
    } finally {
      restoreWarn();
    }

    initTeamCollaborationBoard();
    expect(dispatchInternalMessage()).not.toBeNull();

    disposeTeamCollaborationBoard();
    const restoreWarn2 = silenceUnhandledWarns();
    try {
      expect(dispatchInternalMessage()).toBeNull();
    } finally {
      restoreWarn2();
    }
  });
});
