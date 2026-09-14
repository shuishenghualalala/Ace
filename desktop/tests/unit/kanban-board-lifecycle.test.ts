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
  disposeKanbanBoard,
  initKanbanBoard,
  refreshKanbanBoard,
} from '../../src/ui/features/kanban-board';
import { backendApi, type DynamicKanbanStatus, type Task } from '../../src/ui/backend-client';
import { setActiveSessionId, state } from '../../src/ui/state';
import { __resetAllStoresForTest } from '../../src/ui/stores/stores';

type BoardResponse = Awaited<ReturnType<typeof backendApi.dynamicKanbanBoard>>;

const EMPTY_BOARD = { tasks: [], dependencies: [], events: [] };
const ACTIVE_STATUS = { workflow: { status: 'active' } } as DynamicKanbanStatus;

/** 手工控制 resolve 时机的 board 响应，用于构造「在途迟到」场景。 */
function deferredBoard(): { promise: Promise<BoardResponse>; resolve: (value: BoardResponse) => void } {
  let resolve!: (value: BoardResponse) => void;
  const promise = new Promise<BoardResponse>((res) => {
    resolve = res;
  });
  return { promise, resolve };
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
    const staleBoard = deferredBoard();
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
    const board = deferredBoard();
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
