/**
 * @vitest-environment happy-dom
 *
 * feature_event 流路径语义补齐测试：注入 feature_event 新帧（body 含 feature/event/
 * version/payload）走 applyChunk / reduceChunk，断言与等价旧帧产生相同结果——
 * 看板刷新调度、team_internal 渲染副作用、回合 gate 分类、wiki_changed 早期分支时序、
 * 未登记事件安全忽略。归一入口：chat-reducer.toLegacyEventFrame（与 Gateway 出口
 * 兼容层同一资格集）+ applyChunk 入口统一。
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
  toLegacyEventFrame,
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

function legacyChunk(kind: ChatChunk['kind'], body: Record<string, unknown>, extra: Partial<ChatChunk> = {}): ChatChunk {
  return { kind, body, is_final: false, sequence: 1, session_id: SID, ...extra };
}

function featureChunk(
  feature: string,
  event: string,
  payload: Record<string, unknown>,
  extra: Partial<ChatChunk> = {},
  version = 1,
): ChatChunk {
  return legacyChunk('feature_event', { feature, event, version, payload }, extra);
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

describe('toLegacyEventFrame / normalizeChunk 两代协议归一', () => {
  it('已登记的 v1 事件映射为与 Gateway 出口逐字段等价的旧 kind 帧', () => {
    expect(normalizeChunk(featureChunk('team', 'internal_message', { text: 'hi' }))).toEqual({
      kind: 'team_internal', body: { text: 'hi' }, sequence: 1, session_id: SID,
    });
    expect(normalizeChunk(featureChunk('wiki', 'cards', { pages: [{ id: 'p1' }] }))).toEqual({
      kind: 'wiki_cards', body: { pages: [{ id: 'p1' }] }, sequence: 1, session_id: SID,
    });
    expect(normalizeChunk(featureChunk('wiki', 'changed', { changes: [{ id: 'p1' }] }))).toEqual({
      kind: 'wiki_changed', body: { changes: [{ id: 'p1' }] }, sequence: 1, session_id: SID,
    });
    expect(normalizeChunk(featureChunk('wiki', 'ingest_progress', { stage: 'compile', percent: 50 }))).toEqual({
      kind: 'wiki_ingest_progress', body: { stage: 'compile', percent: 50 }, sequence: 1, session_id: SID,
    });
    // 旧 kanban 帧 body 约定以 event 字段开头，归一时重建（与 Gateway 出口一致）。
    expect(normalizeChunk(featureChunk('kanban', 'started', { workflow_id: 'wf' }))).toEqual({
      kind: 'kanban', body: { event: 'started', workflow_id: 'wf' }, sequence: 1, session_id: SID,
    });
    expect(normalizeChunk(featureChunk('kanban', 'board_changed', {}))).toEqual({
      kind: 'kanban', body: { event: 'board_changed' }, sequence: 1, session_id: SID,
    });
    expect(normalizeChunk(featureChunk('kanban', 'call_completed', { call_id: 'c1' }))).toEqual({
      kind: 'kanban', body: { event: 'call_completed', call_id: 'c1' }, sequence: 1, session_id: SID,
    });
    expect(normalizeChunk(featureChunk('kanban', 'workflow_progress', { workflow_id: 'wf', status: 'running' }))).toEqual({
      kind: 'workflow_progress', body: { workflow_id: 'wf', status: 'running' }, sequence: 1, session_id: SID,
    });
  });

  it('未登记事件、未知版本原样保留 feature_event 形态；非 feature_event 帧原样返回', () => {
    expect(normalizeChunk(featureChunk('future_feature', 'boom', { x: 1 }))?.kind).toBe('feature_event');
    expect(normalizeChunk(featureChunk('wiki', 'changed', {}, {}, 2))?.kind).toBe('feature_event');
    expect(normalizeChunk(featureChunk('kanban', 'started', {}, {}, 3))?.kind).toBe('feature_event');

    const delta = legacyChunk('delta', { text: 'x' });
    expect(toLegacyEventFrame(delta)).toBe(delta);
  });

  it('归一后的 kind 进入与旧帧相同的回合 gate 分类，裸 feature_event 不进 kind 集合', () => {
    expect(TURN_GENERATION_CHUNK_KINDS.has(normalizeChunk(featureChunk('team', 'internal_message', {}))!.kind)).toBe(true);
    for (const [feature, event] of [
      ['wiki', 'cards'],
      ['kanban', 'started'],
      ['kanban', 'board_changed'],
      ['kanban', 'call_completed'],
      ['kanban', 'workflow_progress'],
    ] as const) {
      expect(TURN_SCOPED_CHUNK_KINDS.has(normalizeChunk(featureChunk(feature, event, {}))!.kind)).toBe(true);
    }
    expect(TURN_SCOPED_CHUNK_KINDS.has('feature_event')).toBe(false);
    expect(TURN_GENERATION_CHUNK_KINDS.has('feature_event')).toBe(false);

    // 映射后的 team_internal 具备旧帧的生成帧语义：封口回合迟到帧被拒收。
    const sealed = { turnSealed: true, activeRequestId: null, acceptingNewRequest: false };
    expect(resolveTurnGate(normalizeChunk(featureChunk('team', 'internal_message', {}))!.kind, 'req-1', sealed))
      .toEqual({ action: 'drop' });
  });
});

describe('看板刷新调度（kanban 白名单补齐）', () => {
  // 共享 helper mock 掉了 kanban-board 模块（生产注册点 initKanbanBoard 不可用），
  // 这里注册最小 handler 镜像「已注册」状态，避免 workflow_progress 事件触发 unhandled 警告噪声。
  let disposeWorkflowProgress: (() => void) | null = null;

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
    disposeWorkflowProgress = featureEventRegistry.register({
      feature: 'kanban', event: 'workflow_progress', version: 1,
      reducer: () => emptyFeatureReducerResult(),
    });
  });

  afterEach(() => {
    disposeWorkflowProgress?.();
    disposeWorkflowProgress = null;
  });

  it('kanban 全部事件的 feature_event 帧与旧帧一样触发节流刷新调度', () => {
    const scheduleRefresh = registerKanbanSpies();
    configStore.set({ mode: 'dynamic_kanban' });

    applyChunk(legacyChunk('kanban', { event: 'started' }));
    expect(scheduleRefresh).toHaveBeenCalledTimes(1);
    scheduleRefresh.mockClear();

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

describe('team_internal 渲染副作用（team 分支补齐）', () => {
  beforeEach(() => {
    initTeamCollaborationBoard();
  });

  it('feature_event(team,internal_message) 与旧帧产生相同消息并触发相同副作用集', () => {
    openTurnForRequest(SID, 'req-team');
    const teamBody = {
      agent_name: 'Crew',
      agent_id: 'crew::builtin',
      is_leader: true,
      event_type: 'team_stream',
      node_id: 'build',
    };

    applyChunk(legacyChunk('team_internal', { ...teamBody, text: '旧帧消息' }, { request_id: 'req-team', sequence: 2 }));
    const legacyMessages = messages();
    const callsAfterLegacy = historyCalls();
    expect(legacyMessages.at(-1)).toMatchObject({ role: 'team_internal', content: '旧帧消息', agentName: 'Crew' });
    // team_internal 专属副作用分支：renderWorkspaceHistory 是该分支独有调用，通用合并路径不经过它。
    expect(callsAfterLegacy).toBeGreaterThan(0);

    applyChunk(featureChunk('team', 'internal_message', { ...teamBody, text: '新帧消息' }, { request_id: 'req-team', sequence: 3 }));
    const all = messages();
    expect(all).toHaveLength(legacyMessages.length + 1);
    expect(all.at(-1)).toMatchObject({ role: 'team_internal', content: '新帧消息', agentName: 'Crew' });
    expect(historyCalls()).toBe(callsAfterLegacy + 1);
  });

  it('封口回合迟到的 feature_event(team,internal_message) 与旧帧一样被 gate 丢弃', () => {
    const before = messages();
    const calls = historyCalls();

    applyChunk(legacyChunk('team_internal', { text: '迟到旧帧' }, { request_id: 'req-stale', sequence: 2 }));
    applyChunk(featureChunk('team', 'internal_message', { text: '迟到新帧' }, { request_id: 'req-stale', sequence: 3 }));

    expect(messages()).toEqual(before);
    expect(historyCalls()).toBe(calls);
  });

  it('封口回合迟到的 feature_event(wiki,cards) 与旧帧一样被 gate 丢弃', () => {
    // 安装 wiki handler，保证「gate 未丢弃 → 会写消息」可被观测，测试不空转。
    configStore.set({ config: wikiConfig(true) });
    initWikiAgent();
    const before = messages();
    applyChunk(legacyChunk('wiki_cards', { pages: [{ id: 'p1' }] }, { request_id: 'req-stale', sequence: 2 }));
    applyChunk(featureChunk('wiki', 'cards', { pages: [{ id: 'p2' }] }, { request_id: 'req-stale', sequence: 3 }));
    expect(messages()).toEqual(before);
  });
});

describe('wiki_changed 时序（早期分支等价）', () => {
  beforeEach(() => {
    configStore.set({ config: wikiConfig(true) });
    initWikiAgent();
  });

  it('feature_event(wiki,changed) 与旧帧一样派发 wiki:changed DOM 事件', () => {
    const changed = vi.fn();
    window.addEventListener('wiki:changed', changed);
    try {
      applyChunk(legacyChunk('wiki_changed', { changes: [{ id: 'p1' }] }, { sequence: 2 }));
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

  it('历史加载窗口内 feature_event(wiki,changed) 与旧帧一样立即派发且不入队', () => {
    const changed = vi.fn();
    window.addEventListener('wiki:changed', changed);
    setHistoryLoading(SID, true);
    try {
      applyChunk(featureChunk('wiki', 'changed', { changes: [{ id: 'p1' }] }));
      expect(changed).toHaveBeenCalledTimes(1);
      // 未入队：队列应为空（旧帧同路径，history/sequence 之前处理）。
      expect(flushPendingChunks(SID)).toBeNull();

      // 对照：主管道帧（wiki cards）在加载窗口内确实排队，证明上面的断言不是空转。
      applyChunk(featureChunk('wiki', 'cards', { pages: [{ id: 'p9' }] }));
      const queued = flushPendingChunks(SID);
      expect(queued).toHaveLength(1);
      expect(queued?.[0]?.kind).toBe('wiki_cards');

      applyChunk(legacyChunk('wiki_changed', { changes: [{ id: 'p2' }] }));
      expect(changed).toHaveBeenCalledTimes(2);
    } finally {
      setHistoryLoading(SID, false);
      window.removeEventListener('wiki:changed', changed);
    }
  });

  it('feature_event(wiki,changed) 与旧帧一样不受 gateway sequence 去重影响', () => {
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

  it('feature_event(wiki,cards / ingest_progress) 与旧帧产生相同结果', () => {
    const progress = vi.fn();
    setWikiIngestProgressCallback(progress);
    applyChunk(legacyChunk('wiki_cards', { pages: [{ id: 'p1', title: '旧帧卡片' }] }, { sequence: 2 }));
    applyChunk(featureChunk('wiki', 'ingest_progress', { stage: 'embed', percent: 80, source_id: 's1' }, { sequence: 3 }));

    const carrier = messages();
    expect(carrier).toHaveLength(1);
    expect(carrier[0]?.wikiCards).toMatchObject([{ id: 'p1', title: '旧帧卡片' }]);
    expect(progress).toHaveBeenCalledTimes(1);
    expect(progress.mock.calls[0]?.[0]).toMatchObject({
      stage: 'embed', percent: 80, source_id: 's1', session_id: SID,
    });

    // 新帧 cards 走同一 reducer：patch 到同一载体消息，而非追加第二条。
    applyChunk(featureChunk('wiki', 'cards', { pages: [{ id: 'p2', title: '新帧卡片' }] }, { sequence: 4 }));
    expect(messages()).toHaveLength(1);
    expect(messages()[0]?.wikiCards).toMatchObject([{ id: 'p2', title: '新帧卡片' }]);
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

  it('版本不匹配的已登记事件不映射为旧帧：不触发 DOM 副作用，registry 可诊断忽略', () => {
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
