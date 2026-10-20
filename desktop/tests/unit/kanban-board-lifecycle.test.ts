// @vitest-environment happy-dom

/**
 * Dynamic Kanban 看板生命周期测试：卸载 / 重装 / 切会话时的在途请求守卫。
 *
 * - dispose 后迟到的 board/status/tasks 响应不得回写 state、不得渲染、不得新增轮询 interval；
 * - dispose → init 后上一代请求仍失效，新一代请求正常工作；
 * - 切换会话后迟到响应不得污染新会话看板；
 * - 无卸载的正常路径行为不变（回写 + 渲染 + workflow 活跃时启动轮询）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  __resetKanbanBoardForTest,
  buildKanbanInspectorHtml,
  disposeKanbanBoard,
  initKanbanBoard,
  refreshKanbanBoard,
} from '../../src/ui/features/kanban-board';
import { backendApi, type DynamicKanbanStatus, type Task } from '../../src/ui/backend-client';
import { setActiveSessionId, state } from '../../src/ui/state';
import { __resetAllStoresForTest } from '../../src/ui/stores/stores';
import { emptyFeatureReducerResult, featureEventRegistry } from '../../src/ui/features/event-reducer-registry';
import * as boardHooks from '../../src/ui/features/board-hooks';

type BoardResponse = Awaited<ReturnType<typeof backendApi.dynamicKanbanBoard>>;

const EMPTY_BOARD = { tasks: [], dependencies: [], events: [] };
const ACTIVE_STATUS = { workflow: { status: 'active' } } as DynamicKanbanStatus;
/** 与 kanban-board 模块内 KANBAN_STATUS_POLL_MS 一致。 */
const KANBAN_STATUS_POLL_MS = 2_000;

/** 手工控制 resolve/reject 时机与结果的 deferred，用于构造「在途迟到」时序。 */
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

/** 仅排空微任务队列（确定性的 await 屏障，不依赖定时器）。 */
async function flushMicrotasks(): Promise<void> {
  for (let i = 0; i < 10; i += 1) await Promise.resolve();
}

/** 构造带阶段名标记的 status 快照，便于断言 latestStatus/DOM 归属哪一代。 */
function statusWithPhase(workflowStatus: string, phaseName: string): DynamicKanbanStatus {
  return {
    workflow: { status: workflowStatus },
    workflow_definition: { phases: [{ id: 'p1', name: phaseName }] },
    runtime_state: {
      workflow_id: 'wf-1',
      status: workflowStatus,
      current_phase_id: 'p1',
      completed_phase_ids: [],
    },
    board: { workflow_id: 'wf-1', tasks: [], dependencies: [], events: [] },
  };
}

beforeEach(() => {
  __resetAllStoresForTest();
  __resetKanbanBoardForTest();
  document.body.innerHTML = '<div id="chat-inspector-body"><div id="workflow-timeline-container"></div></div>';
  state.kanbanBoard = { ...EMPTY_BOARD };
  state.tasks = [];
  setActiveSessionId('review-session');
  state.mode = 'dynamic_kanban';
});

afterEach(() => {
  __resetKanbanBoardForTest();
  vi.restoreAllMocks();
  document.body.innerHTML = '';
});

describe('kanban-board 卸载/切会话后在途请求守卫', () => {
  it('unloading must reject late board response and prevent polling restart', async () => {
    let resolveBoard!: (value: BoardResponse) => void;
    const boardPromise = new Promise<BoardResponse>((resolve) => {
      resolveBoard = resolve;
    });
    vi.spyOn(backendApi, 'dynamicKanbanBoard').mockReturnValue(boardPromise);
    vi.spyOn(backendApi, 'dynamicKanbanStatus').mockResolvedValue(ACTIVE_STATUS);
    vi.spyOn(backendApi, 'tasks').mockResolvedValue([]);
    const intervals = vi.spyOn(window, 'setInterval');

    initKanbanBoard();
    const refreshing = refreshKanbanBoard();
    disposeKanbanBoard();
    resolveBoard({ tasks: [{ id: 'late-old-generation' }], dependencies: [], events: [] });
    await refreshing;

    expect(state.kanbanBoard?.tasks).toEqual([]);
    expect(intervals).not.toHaveBeenCalled();
    // 迟到响应不得触发渲染（inspector body 未被重绘为看板内容）
    expect(document.body.innerHTML).not.toContain('kanban-board__badge');
  });

  it('dispose → init 后上一代请求失效，新一代请求正常工作', async () => {
    const staleBoard = deferred<BoardResponse>();
    vi.spyOn(backendApi, 'dynamicKanbanBoard')
      .mockReturnValueOnce(staleBoard.promise)
      .mockResolvedValue({ tasks: [{ id: 'fresh-task' }], dependencies: [], events: [] });
    vi.spyOn(backendApi, 'dynamicKanbanStatus').mockResolvedValue(ACTIVE_STATUS);
    vi.spyOn(backendApi, 'tasks').mockResolvedValue([]);
    const intervals = vi.spyOn(window, 'setInterval');

    initKanbanBoard();
    const stale = refreshKanbanBoard();
    disposeKanbanBoard();
    initKanbanBoard();
    staleBoard.resolve({ tasks: [{ id: 'late-old-generation' }], dependencies: [], events: [] });
    await stale;
    expect(state.kanbanBoard?.tasks).toEqual([]);
    expect(intervals).not.toHaveBeenCalled();

    // 新一代请求正常回写并按 workflow 状态启动轮询
    await refreshKanbanBoard();
    expect(state.kanbanBoard?.tasks).toEqual([{ id: 'fresh-task' }]);
    expect(intervals).toHaveBeenCalledTimes(1);
  });

  it('切换会话后迟到响应不得污染新会话看板', async () => {
    const board = deferred<BoardResponse>();
    vi.spyOn(backendApi, 'dynamicKanbanBoard').mockReturnValue(board.promise);
    vi.spyOn(backendApi, 'dynamicKanbanStatus').mockResolvedValue(ACTIVE_STATUS);
    vi.spyOn(backendApi, 'tasks').mockResolvedValue([]);
    const intervals = vi.spyOn(window, 'setInterval');

    initKanbanBoard();
    const refreshing = refreshKanbanBoard();
    setActiveSessionId('other-session');
    board.resolve({ tasks: [{ id: 'stale-session-task' }], dependencies: [], events: [] });
    await refreshing;

    expect(state.kanbanBoard?.tasks).toEqual([]);
    expect(intervals).not.toHaveBeenCalled();
  });

  it('dispose 停掉已在运行的轮询定时器', async () => {
    vi.spyOn(backendApi, 'dynamicKanbanBoard').mockResolvedValue({ ...EMPTY_BOARD });
    vi.spyOn(backendApi, 'dynamicKanbanStatus').mockResolvedValue(ACTIVE_STATUS);
    vi.spyOn(backendApi, 'tasks').mockResolvedValue([]);
    const intervals = vi.spyOn(window, 'setInterval');

    initKanbanBoard();
    await refreshKanbanBoard();
    expect(intervals).toHaveBeenCalledTimes(1);

    const cleared = vi.spyOn(window, 'clearInterval');
    disposeKanbanBoard();
    expect(cleared).toHaveBeenCalled();
  });
});

describe('kanban-board 状态轮询所有权（代际绑定）', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    // 先还原 spy（其捕获的"原始实现"可能是 fake 定时器），再恢复真实定时器，
    // 避免把 fake 定时器泄漏给后续用例。
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  it('同会话卸载重装后，旧代挂起的轮询响应不得改写状态/DOM，也不得停掉新代定时器', async () => {
    const staleStatus = deferred<DynamicKanbanStatus>();
    vi.spyOn(backendApi, 'dynamicKanbanBoard').mockResolvedValue({ ...EMPTY_BOARD });
    const statusSpy = vi.spyOn(backendApi, 'dynamicKanbanStatus')
      .mockResolvedValueOnce(ACTIVE_STATUS)        // gen1 初始刷新 → 启动轮询
      .mockReturnValueOnce(staleStatus.promise)    // gen1 轮询 tick → 挂起
      .mockResolvedValue(statusWithPhase('active', '新代活跃阶段')); // 重装后刷新与后续 tick
    vi.spyOn(backendApi, 'tasks').mockResolvedValue([]);
    const intervals = vi.spyOn(window, 'setInterval');
    const clears = vi.spyOn(window, 'clearInterval');

    initKanbanBoard();
    await refreshKanbanBoard();
    expect(intervals).toHaveBeenCalledTimes(1);

    vi.advanceTimersByTime(KANBAN_STATUS_POLL_MS); // gen1 轮询 tick：发出挂起的 status 请求
    expect(statusSpy).toHaveBeenCalledTimes(2);

    disposeKanbanBoard();
    initKanbanBoard();
    await refreshKanbanBoard();                    // 新一代刷新 → 新代轮询启动
    expect(intervals).toHaveBeenCalledTimes(2);
    expect(document.body.innerHTML).toContain('新代活跃阶段');
    const htmlBefore = document.body.innerHTML;
    const clearsBefore = clears.mock.calls.length;

    staleStatus.resolve(statusWithPhase('done', '旧代终态阶段')); // 旧代「终态」响应迟到
    await flushMicrotasks();

    // 旧代终态不得停掉新代的轮询定时器
    expect(clears.mock.calls.length).toBe(clearsBefore);
    // 旧代响应不得改写 DOM / latestStatus
    expect(document.body.innerHTML).toBe(htmlBefore);
    expect(buildKanbanInspectorHtml()).toContain('新代活跃阶段');
    expect(buildKanbanInspectorHtml()).not.toContain('旧代终态阶段');
    // 新代轮询仍然存活并继续 tick（3 次调用 = 初始刷新 + gen1 tick + 重装刷新；此为第 4 次）
    vi.advanceTimersByTime(KANBAN_STATUS_POLL_MS);
    expect(statusSpy).toHaveBeenCalledTimes(4);
  });

  it('旧代轮询响应 reject 时同样不得回写、不得停掉新代定时器', async () => {
    const staleStatus = deferred<DynamicKanbanStatus>();
    vi.spyOn(backendApi, 'dynamicKanbanBoard').mockResolvedValue({ ...EMPTY_BOARD });
    const statusSpy = vi.spyOn(backendApi, 'dynamicKanbanStatus')
      .mockResolvedValueOnce(ACTIVE_STATUS)
      .mockReturnValueOnce(staleStatus.promise)
      .mockResolvedValue(statusWithPhase('active', '新代活跃阶段'));
    vi.spyOn(backendApi, 'tasks').mockResolvedValue([]);
    const clears = vi.spyOn(window, 'clearInterval');

    initKanbanBoard();
    await refreshKanbanBoard();
    vi.advanceTimersByTime(KANBAN_STATUS_POLL_MS);
    disposeKanbanBoard();
    initKanbanBoard();
    await refreshKanbanBoard();
    const htmlBefore = document.body.innerHTML;
    const clearsBefore = clears.mock.calls.length;

    staleStatus.reject(new Error('旧代轮询请求失败'));
    await flushMicrotasks();

    expect(clears.mock.calls.length).toBe(clearsBefore);
    expect(document.body.innerHTML).toBe(htmlBefore);
    expect(buildKanbanInspectorHtml()).toContain('新代活跃阶段');

    vi.advanceTimersByTime(KANBAN_STATUS_POLL_MS);
    expect(statusSpy).toHaveBeenCalledTimes(4); // 新代轮询未被失败响应波及
  });

  it('dispose 中止轮询后旧代定时器不再发请求，重装后新一代有自己的定时器', async () => {
    vi.spyOn(backendApi, 'dynamicKanbanBoard').mockResolvedValue({ ...EMPTY_BOARD });
    const statusSpy = vi.spyOn(backendApi, 'dynamicKanbanStatus').mockResolvedValue(ACTIVE_STATUS);
    vi.spyOn(backendApi, 'tasks').mockResolvedValue([]);
    const intervals = vi.spyOn(window, 'setInterval');

    initKanbanBoard();
    await refreshKanbanBoard();
    expect(intervals).toHaveBeenCalledTimes(1);

    disposeKanbanBoard();
    vi.advanceTimersByTime(KANBAN_STATUS_POLL_MS * 3);
    // 旧代定时器随 dispose 终止：不再发出任何轮询请求
    expect(statusSpy).toHaveBeenCalledTimes(1);

    initKanbanBoard();
    await refreshKanbanBoard();
    expect(intervals).toHaveBeenCalledTimes(2); // 每代至多一个定时器
    vi.advanceTimersByTime(KANBAN_STATUS_POLL_MS);
    expect(statusSpy).toHaveBeenCalledTimes(3); // 新代 tick 正常（2 次刷新 + 1 次 tick）
  });

  it('切会话后旧会话挂起的轮询响应不得停掉新会话轮询或改写新会话状态', async () => {
    const staleStatus = deferred<DynamicKanbanStatus>();
    vi.spyOn(backendApi, 'dynamicKanbanBoard').mockResolvedValue({ ...EMPTY_BOARD });
    const statusSpy = vi.spyOn(backendApi, 'dynamicKanbanStatus')
      .mockResolvedValueOnce(statusWithPhase('active', '旧会话阶段'))  // 会话 A 初始刷新
      .mockReturnValueOnce(staleStatus.promise)                        // 会话 A 轮询 tick 挂起
      .mockResolvedValue(statusWithPhase('active', '新会话阶段'));     // 会话 B 刷新与后续 tick
    vi.spyOn(backendApi, 'tasks').mockResolvedValue([]);
    const clears = vi.spyOn(window, 'clearInterval');

    initKanbanBoard();
    setActiveSessionId('session-a');
    await refreshKanbanBoard();
    vi.advanceTimersByTime(KANBAN_STATUS_POLL_MS);
    expect(statusSpy).toHaveBeenCalledTimes(2);

    setActiveSessionId('session-b');
    await refreshKanbanBoard(); // B 会话刷新：停 A 定时器、启动 B 定时器
    expect(document.body.innerHTML).toContain('新会话阶段');
    const clearsBefore = clears.mock.calls.length;

    staleStatus.resolve(statusWithPhase('done', '旧会话终态阶段'));
    await flushMicrotasks();

    // B 会话的轮询不被旧会话迟到响应停止，状态也不被改写
    expect(clears.mock.calls.length).toBe(clearsBefore);
    expect(buildKanbanInspectorHtml()).toContain('新会话阶段');
    expect(buildKanbanInspectorHtml()).not.toContain('旧会话终态阶段');
    vi.advanceTimersByTime(KANBAN_STATUS_POLL_MS);
    expect(statusSpy).toHaveBeenCalledTimes(4); // B 轮询存活（2 次刷新 + 2 次 tick）
  });
});

describe('kanban-board 正常路径（无卸载）行为不变', () => {
  it('回写 board/tasks、渲染看板、workflow 活跃时启动轮询', async () => {
    vi.spyOn(backendApi, 'dynamicKanbanBoard').mockResolvedValue({
      tasks: [{ id: 'kanban-1', status: 'ready' }],
      dependencies: [],
      events: [],
    });
    vi.spyOn(backendApi, 'dynamicKanbanStatus').mockResolvedValue(ACTIVE_STATUS);
    vi.spyOn(backendApi, 'tasks').mockResolvedValue([
      { id: 'turn-1', task_id: 'turn-1', kind: 'agent_turn', title: '整轮对话容器', status: 'running' },
      { id: 'shell-1', task_id: 'shell-1', kind: 'shell', title: '后台构建脚本', status: 'running' },
    ] as Task[]);
    const intervals = vi.spyOn(window, 'setInterval');

    initKanbanBoard();
    await refreshKanbanBoard();

    // 状态回写
    expect(state.kanbanBoard?.tasks).toEqual([{ id: 'kanban-1', status: 'ready' }]);
    // agent_turn 容器任务被过滤，shell 任务保留
    expect(state.tasks.map((t) => t.id)).toEqual(['shell-1']);
    // 渲染：inspector body 被重绘为看板内容，含状态徽标与后台任务
    const body = document.getElementById('chat-inspector-body');
    expect(body?.innerHTML).toContain('kanban-board__badge');
    expect(body?.innerHTML).toContain('后台构建脚本');
    expect(body?.innerHTML).not.toContain('整轮对话容器');
    // workflow 活跃 → 轮询启动
    expect(intervals).toHaveBeenCalledTimes(1);
  });
});

describe('kanban-board 安装事务回滚（单 Feature 事务）', () => {
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

  function dispatchWorkflowProgress(): ReturnType<typeof featureEventRegistry.dispatch> {
    return featureEventRegistry.dispatch(
      'kanban',
      'workflow_progress',
      1,
      { workflow_id: 'wf-1', status: 'running' },
      dummyCtx,
    );
  }

  /** registry 未命中会 console.warn 噪声，断言「无残留」时抑制。 */
  function silenceUnhandledWarns(): () => void {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    return () => warn.mockRestore();
  }

  it('中途 reducer 注册失败：本次已注册的贡献逆序回滚，既有注册不受影响，重试成功且重复 dispose 幂等', () => {
    const external = featureEventRegistry.register({
      feature: 'external',
      event: 'ping',
      version: 1,
      reducer: () => emptyFeatureReducerResult(),
    });
    const original = featureEventRegistry.register.bind(featureEventRegistry);
    let calls = 0;
    const spy = vi.spyOn(featureEventRegistry, 'register').mockImplementation((reg) => {
      calls += 1;
      if (calls === 2) throw new Error('注入：第 2 次注册失败');
      return original(reg);
    });

    expect(() => initKanbanBoard()).toThrow('注入：第 2 次注册失败');

    let restoreWarn = silenceUnhandledWarns();
    try {
      // 第 1 次成功注册的 workflow_progress reducer 已回滚：registry 中无本次残留
      expect(dispatchWorkflowProgress()).toBeNull();
      // 安装前已存在的贡献不受影响、仍可用
      expect(featureEventRegistry.dispatch('external', 'ping', 1, {}, dummyCtx)).not.toBeNull();
    } finally {
      restoreWarn();
    }

    // 解除故障后重试安装成功（旧实现此处会撞 already registered）
    spy.mockRestore();
    const disposer = initKanbanBoard();
    expect(dispatchWorkflowProgress()).not.toBeNull();

    // 重复 dispose 幂等：二次清理无异常，且清理后事件不再派发
    disposer();
    disposer();
    restoreWarn = silenceUnhandledWarns();
    try {
      expect(dispatchWorkflowProgress()).toBeNull();
    } finally {
      restoreWarn();
    }
    external();
  });

  it('hooks 注册失败：已注册的 reducer 与 resize 监听器全部回滚，安装过程无定时器残留，重试成功', () => {
    document.body.innerHTML += '<div id="task-board-resize-handle"></div>';
    const addSpy = vi.spyOn(window, 'addEventListener');
    const removeSpy = vi.spyOn(window, 'removeEventListener');
    const intervals = vi.spyOn(window, 'setInterval');
    vi.spyOn(boardHooks, 'registerKanbanBoardCallbacks').mockImplementationOnce(() => {
      throw new Error('注入：hooks 注册失败');
    });

    expect(() => initKanbanBoard()).toThrow('注入：hooks 注册失败');

    const restoreWarn = silenceUnhandledWarns();
    try {
      // 4 个 reducer 全部回滚：registry 无 kanban 残留
      expect(dispatchWorkflowProgress()).toBeNull();
      expect(featureEventRegistry.dispatch('kanban', 'started', 1, {}, dummyCtx)).toBeNull();
      // resize 的 window 监听器已逆序撤销：mousemove 的 add/remove 对称
      const added = addSpy.mock.calls.filter(([type]) => type === 'mousemove').length;
      const removed = removeSpy.mock.calls.filter(([type]) => type === 'mousemove').length;
      expect(added).toBeGreaterThan(0);
      expect(removed).toBe(added);
      // 安装失败全程无定时器产生
      expect(intervals).not.toHaveBeenCalled();
    } finally {
      restoreWarn();
    }

    // 重试成功
    const disposer = initKanbanBoard();
    expect(dispatchWorkflowProgress()).not.toBeNull();
    disposer();
  });
});
