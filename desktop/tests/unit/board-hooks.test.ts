/**
 * board-hooks 门面与看板生命周期测试。
 *
 * 覆盖：
 * - 门面未注册时 no-op；
 * - 注册后中心调用点参数转发一致；
 * - kanban / team init 幂等、dispose 后 reducer 不再注册；
 * - dispose 释放计时器与状态。
 *
 * @vitest-environment happy-dom
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  primeTeamCollaborationIdentity,
  refreshKanbanBoard,
  registerKanbanBoardCallbacks,
  registerTeamBoardCallbacks,
  renderKanbanBoard,
  scheduleRefreshKanbanBoard,
} from '../../src/ui/features/board-hooks';
import { featureEventRegistry } from '../../src/ui/features/event-reducer-registry';
import {
  disposeKanbanBoard,
  initKanbanBoard,
  __resetKanbanBoardForTest,
} from '../../src/ui/features/kanban-board';
import {
  disposeTeamCollaborationBoard,
  initTeamCollaborationBoard,
  startTeamCollaborationPolling,
  __resetTeamCollaborationBoardForTest,
} from '../../src/ui/features/team-collaboration-board';

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

describe('board-hooks 门面', () => {
  afterEach(() => {
    __resetKanbanBoardForTest();
    __resetTeamCollaborationBoardForTest();
    vi.restoreAllMocks();
  });

  it('未注册 kanban 回调时门面调用为 no-op', async () => {
    await expect(refreshKanbanBoard('s1')).resolves.toBeUndefined();
    expect(() => scheduleRefreshKanbanBoard('s1')).not.toThrow();
    expect(() => renderKanbanBoard()).not.toThrow();
  });

  it('未注册 team 回调时门面调用为 no-op', async () => {
    await expect(primeTeamCollaborationIdentity('s1')).resolves.toBeUndefined();
  });

  it('kanban 回调注册后中心调用点参数转发一致', async () => {
    const refresh = vi.fn().mockResolvedValue(undefined);
    const scheduleRefresh = vi.fn();
    const render = vi.fn();
    const dispose = registerKanbanBoardCallbacks({ refresh, scheduleRefresh, render });

    await refreshKanbanBoard('sid-1');
    scheduleRefreshKanbanBoard('sid-2');
    renderKanbanBoard();

    expect(refresh).toHaveBeenCalledTimes(1);
    expect(refresh).toHaveBeenCalledWith('sid-1');
    expect(scheduleRefresh).toHaveBeenCalledTimes(1);
    expect(scheduleRefresh).toHaveBeenCalledWith('sid-2');
    expect(render).toHaveBeenCalledTimes(1);

    dispose();
  });

  it('team 回调注册后中心调用点参数转发一致', async () => {
    const primeTeamIdentity = vi.fn().mockResolvedValue(undefined);
    const dispose = registerTeamBoardCallbacks({ primeTeamIdentity });

    await primeTeamCollaborationIdentity('sid-3');

    expect(primeTeamIdentity).toHaveBeenCalledTimes(1);
    expect(primeTeamIdentity).toHaveBeenCalledWith('sid-3');

    dispose();
  });
});

describe('Dynamic Kanban 生命周期', () => {
  beforeEach(() => {
    __resetKanbanBoardForTest();
  });
  afterEach(() => {
    __resetKanbanBoardForTest();
  });

  it('initKanbanBoard 幂等且返回同一 disposer', () => {
    const d1 = initKanbanBoard();
    const d2 = initKanbanBoard();
    expect(d1).toBe(d2);
  });

  it('init 后 workflow_progress reducer 可 dispatch，dispose 后返回 null', () => {
    initKanbanBoard();
    const hit = featureEventRegistry.dispatch(
      'kanban',
      'workflow_progress',
      1,
      { workflow_id: 'wf-1', status: 'running' },
      dummyCtx,
    );
    expect(hit).not.toBeNull();

    disposeKanbanBoard();
    const miss = featureEventRegistry.dispatch(
      'kanban',
      'workflow_progress',
      1,
      { workflow_id: 'wf-2', status: 'running' },
      dummyCtx,
    );
    expect(miss).toBeNull();
  });

  it('dispose 后再次 init 可重新注册 reducer', () => {
    initKanbanBoard();
    disposeKanbanBoard();
    initKanbanBoard();
    const hit = featureEventRegistry.dispatch(
      'kanban',
      'workflow_progress',
      1,
      { workflow_id: 'wf-3', status: 'running' },
      dummyCtx,
    );
    expect(hit).not.toBeNull();
  });

  it('dispose 清理 kanban 刷新定时器与轮询', () => {
    vi.useFakeTimers();
    initKanbanBoard();
    // 通过门面触发一次节流刷新，产生一个 setTimeout
    scheduleRefreshKanbanBoard('s1');
    const clearTimeoutSpy = vi.spyOn(window, 'clearTimeout');
    const clearIntervalSpy = vi.spyOn(window, 'clearInterval');

    disposeKanbanBoard();

    expect(clearTimeoutSpy).toHaveBeenCalled();
    clearTimeoutSpy.mockRestore();
    clearIntervalSpy.mockRestore();
    vi.useRealTimers();
  });
});

describe('Team Collaboration 生命周期', () => {
  beforeEach(() => {
    __resetTeamCollaborationBoardForTest();
  });
  afterEach(() => {
    __resetTeamCollaborationBoardForTest();
  });

  it('initTeamCollaborationBoard 幂等且返回同一 disposer', () => {
    const d1 = initTeamCollaborationBoard();
    const d2 = initTeamCollaborationBoard();
    expect(d1).toBe(d2);
  });

  it('init 后 internal_message reducer 可 dispatch，dispose 后返回 null', () => {
    initTeamCollaborationBoard();
    const hit = featureEventRegistry.dispatch(
      'team',
      'internal_message',
      1,
      { text: 'hello' },
      dummyCtx,
    );
    expect(hit).not.toBeNull();

    disposeTeamCollaborationBoard();
    const miss = featureEventRegistry.dispatch(
      'team',
      'internal_message',
      1,
      { text: 'hello' },
      dummyCtx,
    );
    expect(miss).toBeNull();
  });

  it('dispose 清理 team 轮询', () => {
    initTeamCollaborationBoard();
    startTeamCollaborationPolling('s1');
    const clearIntervalSpy = vi.spyOn(window, 'clearInterval');

    disposeTeamCollaborationBoard();

    expect(clearIntervalSpy).toHaveBeenCalled();
    clearIntervalSpy.mockRestore();
  });
});
