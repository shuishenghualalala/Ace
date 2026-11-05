/**
 * @vitest-environment happy-dom
 *
 * feature_event 流管线语义测试：业务事件只以 feature_event 帧（body 含 feature/event/
 * version/payload）到达。覆盖四条原生行为路径——kanban 刷新调度、team_internal 渲染
 * 副作用、回合 gate 按 (feature,event) 分类、wiki_changed 早期分支时序——以及未登记
 * 事件的可诊断忽略。判定源：chat-reducer.featureEventFrameOf + featureEventTurnSemantics，
 * feature_event 是内部管线的唯一协议形态（6D 归一层已删除）。
 */
import './helpers/mock-chat-controller-deps';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ChatChunk } from '../../src/ui/backend-client';
import { registerKanbanBoardCallbacks } from '../../src/ui/features/board-hooks';
import {
  emptyFeatureReducerResult,
  featureEventRegistry,
} from '../../src/ui/features/event-reducer-registry';
import {
  applyChunk,
  flushPendingChunks,
  _resetTurnDurationTickerForTests,
  setHistoryLoading,
  setWikiIngestProgressCallback,
} from '../../src/ui/features/chat-controller';
import {
  disposeTeamCollaborationBoard,
  initTeamCollaborationBoard,
} from '../../src/ui/features/team-collaboration-board';
import {
  __resetWikiAgentForTest,
  disposeWikiAgentFeature,
  initWikiAgent,
} from '../../src/ui/features/wiki-agent';
import {
  TURN_GENERATION_CHUNK_KINDS,
  TURN_SCOPED_CHUNK_KINDS,
  normalizeChunk,
  resolveTurnGate,
} from '../../src/ui/reducers/chat-reducer';
import { openTurnForRequest } from '../../src/ui/features/session-busy';
import { __resetAllStoresForTest, configStore, messageStore } from '../../src/ui/stores/stores';
import { setActiveSessionId } from '../../src/ui/state';
import { renderWorkspaceHistory } from '../../src/ui/features/workspaces';

vi.mock('../../src/ui/features/workspaces', () => ({
  refreshAllSessions: vi.fn(async () => undefined),
  renderWorkspaceHistory: vi.fn(),
  commitDraftSession: vi.fn(),
  createSessionInWorkspace: vi.fn(() => 'sid-1'),
  getSessionAgentDisplay: vi.fn(() => null),
  isDraftSession: vi.fn(() => false),
}));

const SID = 'sid-1';
const DOM_STUB = '<div id="chat-messages"></div><div id="composer-controls"></div><div class="chat-running-intro"></div>';

function featureChunk(
  feature: string,
  event: string,
  payload: Record<string, unknown>,
  extra: Partial<ChatChunk> = {},
  version = 1,
): ChatChunk {
  return {
    kind: 'feature_event',
    body: { feature, event, version, payload },
    is_final: false,
    sequence: 1,
    session_id: SID,
    ...extra,
  };
}

function messages() {
  return messageStore.get().messages[SID] ?? [];
}

function historyCalls(): number {
  return vi.mocked(renderWorkspaceHistory).mock.calls.length;
}

function wikiConfig(enabled: boolean) {
  return { wiki: { enabled } } as Parameters<typeof configStore.set>[0]['config'];
}

let disposeKanbanCallbacks: (() => void) | null = null;

beforeEach(() => {
  disposeWikiAgentFeature();
  __resetWikiAgentForTest();
  __resetAllStoresForTest();
  document.body.innerHTML = DOM_STUB;
  setActiveSessionId(SID);
  vi.mocked(renderWorkspaceHistory).mockClear();
});

afterEach(() => {
  setWikiIngestProgressCallback(null);
  disposeWikiAgentFeature();
  disposeTeamCollaborationBoard();
  disposeKanbanCallbacks?.();
  disposeKanbanCallbacks = null;
  _resetTurnDurationTickerForTests();
});

describe('normalizeChunk：feature_event 原生保留', () => {
  it('feature_event 帧原样构造 FeatureEventChunk，body 逐字段保留', () => {
    expect(normalizeChunk(featureChunk('team', 'internal_message', { text: 'hi' }))).toEqual({
      kind: 'feature_event',
      body: { feature: 'team', event: 'internal_message', version: 1, payload: { text: 'hi' } },
      sequence: 1,
      session_id: SID,
    });
  });

  it('裸 feature_event 不进回合 kind 集合，分类只按 (feature,event)', () => {
    expect(TURN_SCOPED_CHUNK_KINDS.has('feature_event')).toBe(false);
    expect(TURN_GENERATION_CHUNK_KINDS.has('feature_event')).toBe(false);
  });

  it('feature_event 按 (feature,event) 获得回合 gate 语义', () => {
    // 封口回合 + 活跃 request 已知：generation 帧拒收，turn-scoped 附属帧放行。
    const sealedWithRequest = { turnSealed: true, activeRequestId: 'req-1', acceptingNewRequest: false };
    // team.internal_message → generation 语义：封口回合迟到帧拒收。
    expect(resolveTurnGate(normalizeChunk(featureChunk('team', 'internal_message', {}))!, 'req-1', sealedWithRequest))
      .toEqual({ action: 'drop' });
    // wiki.cards 与 kanban 全部事件 → turn-scoped 语义：同回合附属帧不因封口被拒收。
    for (const [feature, event] of [
      ['wiki', 'cards'],
      ['kanban', 'started'],
      ['kanban', 'board_changed'],
      ['kanban', 'call_completed'],
      ['kanban', 'workflow_progress'],
    ] as const) {
      expect(resolveTurnGate(normalizeChunk(featureChunk(feature, event, {}))!, 'req-1', sealedWithRequest))
        .toEqual({ action: 'accept' });
    }
    // owner 级带外事件与未知事件 → bypass：不参与回合 gate，request 不匹配也不拒收。
    const openWithOtherRequest = { turnSealed: false, activeRequestId: 'req-active', acceptingNewRequest: false };
    expect(resolveTurnGate(normalizeChunk(featureChunk('wiki', 'changed', {}))!, 'req-other', openWithOtherRequest))
      .toEqual({ action: 'accept' });
    expect(resolveTurnGate(normalizeChunk(featureChunk('future_feature', 'boom', {}))!, 'req-other', openWithOtherRequest))
      .toEqual({ action: 'accept' });
  });
});

describe('看板刷新调度（kanban 事件白名单）', () => {
  // 共享 helper mock 掉了 kanban-board 模块（生产注册点 initKanbanBoard 不可用），
  // 这里注册与生产一致的最小 handler 集：workflow_progress 空实现 + 三个无消息变更
  // 事件的显式空 reducer，避免 registry 的 unhandled 警告噪声。
  let disposers: Array<() => void> = [];

  function registerKanbanSpies() {
    const scheduleRefresh = vi.fn();
    disposeKanbanCallbacks = registerKanbanBoardCallbacks({
      refresh: vi.fn(async () => undefined),
      scheduleRefresh,
      render: vi.fn(),
    });
    return scheduleRefresh;
  }

  beforeEach(() => {
    disposers.push(featureEventRegistry.register({
      feature: 'kanban', event: 'workflow_progress', version: 1,
      reducer: () => emptyFeatureReducerResult(),
    }));
    for (const event of ['started', 'board_changed', 'call_completed'] as const) {
      disposers.push(featureEventRegistry.register({
        feature: 'kanban', event, version: 1,
        reducer: () => emptyFeatureReducerResult(),
      }));
    }
  });

  afterEach(() => {
    for (const dispose of disposers) dispose();
    disposers = [];
  });

  it('kanban 全部 4 个事件的 feature_event 帧触发节流刷新调度', () => {
    const scheduleRefresh = registerKanbanSpies();
    configStore.set({ mode: 'dynamic_kanban' });

    for (const event of ['started', 'board_changed', 'call_completed']) {
      applyChunk(featureChunk('kanban', event, { workflow_id: 'wf-1' }));
    }
    applyChunk(featureChunk('kanban', 'workflow_progress', { workflow_id: 'wf-1', status: 'running' }));
    expect(scheduleRefresh).toHaveBeenCalledTimes(4);
  });

  it('保留既有会话守卫：非看板会话 / 非活跃会话不触发刷新', () => {
    const scheduleRefresh = registerKanbanSpies();
    configStore.set({ mode: 'agent' });
    applyChunk(featureChunk('kanban', 'started', {}));
    expect(scheduleRefresh).not.toHaveBeenCalled();

    configStore.set({ mode: 'dynamic_kanban' });
    applyChunk({ ...featureChunk('kanban', 'started', {}), session_id: 'sid-other' });
    expect(scheduleRefresh).not.toHaveBeenCalled();
  });
});

describe('team_internal 渲染副作用（team 分支）', () => {
  beforeEach(() => {
    initTeamCollaborationBoard();
  });

  it('feature_event(team,internal_message) 写入 team_internal 消息并触发副作用集', () => {
    openTurnForRequest(SID, 'req-team');
    const teamBody = {
      agent_name: 'Crew',
      agent_id: 'crew::builtin',
      is_leader: true,
      event_type: 'team_stream',
      node_id: 'build',
    };

    applyChunk(featureChunk('team', 'internal_message', { ...teamBody, text: '团队消息' }, { request_id: 'req-team', sequence: 2 }));
    expect(messages().at(-1)).toMatchObject({ role: 'team_internal', content: '团队消息', agentName: 'Crew' });
    // team_internal 专属副作用分支：renderWorkspaceHistory 是该分支独有调用，通用合并路径不经过它。
    expect(historyCalls()).toBeGreaterThan(0);
  });

  it('封口回合迟到的 feature_event(team,internal_message) 被 gate 丢弃', () => {
    const before = messages();
    const calls = historyCalls();

    applyChunk(featureChunk('team', 'internal_message', { text: '迟到帧' }, { request_id: 'req-stale', sequence: 2 }));

    expect(messages()).toEqual(before);
    expect(historyCalls()).toBe(calls);
  });

  it('封口回合迟到的 feature_event(wiki,cards) 被 gate 丢弃', () => {
    // 安装 wiki handler，保证「gate 未丢弃 → 会写消息」可被观测，测试不空转。
    configStore.set({ config: wikiConfig(true) });
    initWikiAgent();
    const before = messages();
    applyChunk(featureChunk('wiki', 'cards', { pages: [{ id: 'p2' }] }, { request_id: 'req-stale', sequence: 3 }));
    expect(messages()).toEqual(before);
  });
});

describe('wiki_changed 时序（早期分支）', () => {
  beforeEach(() => {
    configStore.set({ config: wikiConfig(true) });
    initWikiAgent();
  });

  it('feature_event(wiki,changed) 派发 wiki:changed DOM 事件', () => {
    const changed = vi.fn();
    window.addEventListener('wiki:changed', changed);
    try {
      applyChunk(featureChunk('wiki', 'changed', { changes: [{ id: 'p1' }] }, { sequence: 2 }));
      applyChunk(featureChunk('wiki', 'changed', { changes: [{ id: 'p2' }] }, { sequence: 3 }));

      expect(changed).toHaveBeenCalledTimes(2);
      expect((changed.mock.calls[0]?.[0] as CustomEvent).detail).toMatchObject({
        sessionId: SID, changes: [{ id: 'p1' }],
      });
      expect((changed.mock.calls[1]?.[0] as CustomEvent).detail).toMatchObject({
        sessionId: SID, changes: [{ id: 'p2' }],
      });
    } finally {
      window.removeEventListener('wiki:changed', changed);
    }
  });

  it('历史加载窗口内 feature_event(wiki,changed) 立即派发且不入队', () => {
    const changed = vi.fn();
    window.addEventListener('wiki:changed', changed);
    setHistoryLoading(SID, true);
    try {
      applyChunk(featureChunk('wiki', 'changed', { changes: [{ id: 'p1' }] }));
      expect(changed).toHaveBeenCalledTimes(1);
      // 未入队：队列应为空（owner 级广播在 history/sequence 之前处理）。
      expect(flushPendingChunks(SID)).toBeNull();

      // 对照：主管道帧（wiki cards）在加载窗口内确实排队，证明上面的断言不是空转。
      applyChunk(featureChunk('wiki', 'cards', { pages: [{ id: 'p9' }] }));
      const queued = flushPendingChunks(SID);
      expect(queued).toHaveLength(1);
      expect(queued?.[0]?.kind).toBe('feature_event');

      applyChunk(featureChunk('wiki', 'changed', { changes: [{ id: 'p2' }] }));
      expect(changed).toHaveBeenCalledTimes(2);
    } finally {
      setHistoryLoading(SID, false);
      window.removeEventListener('wiki:changed', changed);
    }
  });

  it('feature_event(wiki,changed) 不受 gateway sequence 去重影响', () => {
    const changed = vi.fn();
    window.addEventListener('wiki:changed', changed);
    try {
      applyChunk(featureChunk('wiki', 'changed', { changes: [{ id: 'p1' }] }, { gateway_sequence: 42 }));
      applyChunk(featureChunk('wiki', 'changed', { changes: [{ id: 'p2' }] }, { gateway_sequence: 42 }));
      expect(changed).toHaveBeenCalledTimes(2);
    } finally {
      window.removeEventListener('wiki:changed', changed);
    }
  });

  it('feature_event(wiki,cards / ingest_progress) 各自驱动卡片载体与进度回调', () => {
    const progress = vi.fn();
    setWikiIngestProgressCallback(progress);
    applyChunk(featureChunk('wiki', 'cards', { pages: [{ id: 'p1', title: '卡片' }] }, { sequence: 2 }));
    applyChunk(featureChunk('wiki', 'ingest_progress', { stage: 'embed', percent: 80, source_id: 's1' }, { sequence: 3 }));

    const carrier = messages();
    expect(carrier).toHaveLength(1);
    expect(carrier[0]?.wikiCards).toMatchObject([{ id: 'p1', title: '卡片' }]);
    expect(progress).toHaveBeenCalledTimes(1);
    expect(progress.mock.calls[0]?.[0]).toMatchObject({
      stage: 'embed', percent: 80, source_id: 's1', session_id: SID,
    });

    // cards 再到：patch 到同一载体消息，而非追加第二条。
    applyChunk(featureChunk('wiki', 'cards', { pages: [{ id: 'p2', title: '新卡片' }] }, { sequence: 4 }));
    expect(messages()).toHaveLength(1);
    expect(messages()[0]?.wikiCards).toMatchObject([{ id: 'p2', title: '新卡片' }]);
  });
});

describe('未知 feature_event 可诊断忽略', () => {
  it('未登记事件安全忽略：console.warn、不抛错、不写消息', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    try {
      const before = messages();
      expect(() => applyChunk(featureChunk('future_feature', 'boom', { x: 1 }, { sequence: 2 }))).not.toThrow();
      expect(messages()).toEqual(before);
      expect(warn).toHaveBeenCalledWith(expect.stringContaining('unhandled event'));
      expect(warn).toHaveBeenCalledWith(expect.stringContaining('feature=future_feature'));
    } finally {
      warn.mockRestore();
    }
  });

  it('版本不匹配的已登记事件不触发 DOM 副作用，registry 可诊断忽略', () => {
    const changed = vi.fn();
    window.addEventListener('wiki:changed', changed);
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    try {
      applyChunk(featureChunk('wiki', 'changed', { changes: [{ id: 'p1' }] }, { sequence: 2 }, 2));
      expect(changed).not.toHaveBeenCalled();
      expect(warn).toHaveBeenCalledWith(expect.stringContaining('version=2'));
    } finally {
      window.removeEventListener('wiki:changed', changed);
      warn.mockRestore();
    }
  });
});
