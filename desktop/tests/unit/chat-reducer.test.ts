/**
 * chat-reducer 单测：覆盖 7 个 kind + unknown 兜底。
 */

import { describe, it, expect } from 'vitest';
import {
  reduceChunk,
  normalizeChunk,
  deltaReducer,
  thinkingReducer,
  toolReducer,
  statusReducer,
  finalReducer,
  applyOrderedDelta,
  resolveFinalContent,
  errorReducer,
  planReviewReducer,
  todoUpdatedReducer,
  workflowProgressReducer,
  followupQuestionReducer,
  type ReducerSnapshot,
} from '../../src/ui/reducers/chat-reducer';
import type { Bookkeeping, FileChange } from '../../src/ui/state';
import type { ChatMessage } from '../../src/ui/chat-render';

function emptyBook(): Bookkeeping {
  return {
    toolMap: new Map(),
    assistantId: null,
    firstChunkAt: null,
    hadTeamInternal: false,
    planActive: false,
    pendingPlan: null,
    pendingFollowup: null,
    todos: [],
    fileChanges: [],
    prevTurnFileSignature: null,
    deltaSpans: [],
    legacyDeltaText: '',
    turnSealed: false,
    activeRequestId: null,
    acceptingNewRequest: false,
  };
}

function makeSnapshot(overrides: Partial<ReducerSnapshot> = {}): ReducerSnapshot {
  return {
    sessionId: 'sid-1',
    messages: [],
    book: emptyBook(),
    currentStatus: 'idle',
    now: 1_700_000_000_000,
    ...overrides,
  };
}

describe('normalizeChunk', () => {
  it('returns null for null', () => {
    expect(normalizeChunk(null)).toBeNull();
  });
  it('returns null for non-object', () => {
    expect(normalizeChunk('string')).toBeNull();
  });
  it('returns null for unknown kind', () => {
    expect(normalizeChunk({ kind: 'mystery', body: {}, sequence: 1 })).toBeNull();
  });
  it('parses delta chunk', () => {
    const c = normalizeChunk({ kind: 'delta', body: { text: 'hi' }, sequence: 1, session_id: 's' });
    expect(c).not.toBeNull();
    expect(c?.kind).toBe('delta');
  });
  it('preserves team_internal chunks for the Desktop team renderer', () => {
    const c = normalizeChunk({
      kind: 'team_internal',
      body: { agent_id: 'hermes', event_type: 'team_stream', text: 'working' },
      sequence: 2,
      session_id: 'team-session',
    });
    expect(c).toMatchObject({
      kind: 'team_internal',
      body: { agent_id: 'hermes', event_type: 'team_stream', text: 'working' },
      sequence: 2,
      session_id: 'team-session',
    });
  });
  it('defaults sequence to 0 and session_id to undefined', () => {
    const c = normalizeChunk({ kind: 'delta', body: {} });
    expect(c).not.toBeNull();
    if (c && c.kind === 'delta') {
      expect(c.sequence).toBe(0);
      expect(c.session_id).toBeUndefined();
    }
  });
});

describe('followupQuestionReducer', () => {
  it('preserves permission side-channel and free-text boundaries', () => {
    const result = followupQuestionReducer({
      kind: 'followup_question',
      sequence: 1,
      body: {
        question_id: 'permission-1',
        title: '权限确认 · browser_use',
        record_history: false,
        questions: [{
          id: 'perm',
          question: '即将执行：press Enter',
          options: [{ label: '允许一次', value: 'allow_once' }],
          allowFreeText: false,
          multiSelect: false,
        }],
      },
    }, makeSnapshot());

    expect(result.replaceBook?.pendingFollowup).toMatchObject({
      questionId: 'permission-1',
      recordHistory: false,
      questions: [{ allowFreeText: false }],
    });
  });

  it('preserves the structured runtime staffing variant and lifecycle state', () => {
    const initial = followupQuestionReducer({
      kind: 'followup_question',
      sequence: 1,
      body: {
        question_id: 'staffing-1',
        title: '给这项任务找一位帮手？',
        note: '仅用于本次任务，不会加入或修改原团队。',
        record_history: false,
        origin: {
          type: 'team_control',
          agent_name: 'Leader',
          mention_intent: 'runtime_staffing',
        },
        questions: [{
          id: 'runtime_staffing:req-1',
          question: '当前成员暂时无法使用。',
          options: [{ label: '现有协作助手', value: 'candidate:0' }],
          allowFreeText: false,
          multiSelect: false,
        }],
      },
    }, makeSnapshot());
    const pending = initial.replaceBook?.pendingFollowup;
    expect(pending).toMatchObject({
      questionId: 'staffing-1',
      note: '仅用于本次任务，不会加入或修改原团队。',
      origin: { mentionIntent: 'runtime_staffing' },
    });

    const applying = followupQuestionReducer({
      kind: 'followup_question',
      sequence: 2,
      body: {
        question_id: 'staffing-1',
        status: 'resolved',
      },
    }, makeSnapshot({ book: { ...emptyBook(), pendingFollowup: pending ?? null } }));
    expect(applying.replaceBook?.pendingFollowup?.status).toBe('applying');

    const applied = followupQuestionReducer({
      kind: 'followup_question',
      sequence: 3,
      body: {
        question_id: 'staffing-1',
        status: 'applied',
        note: '协作助手已加入，继续开工。',
      },
    }, makeSnapshot({ book: applying.replaceBook ?? emptyBook() }));
    expect(applied.replaceBook?.pendingFollowup).toMatchObject({
      status: 'applied',
      note: '协作助手已加入，继续开工。',
    });
  });

  it('keeps the existing resolved behavior for non-staffing permissions', () => {
    const permission = {
      questionId: 'permission-1',
      title: '权限确认',
      recordHistory: false,
      questions: [],
    };
    const result = followupQuestionReducer({
      kind: 'followup_question',
      sequence: 2,
      body: { question_id: 'permission-1', status: 'resolved' },
    }, makeSnapshot({ book: { ...emptyBook(), pendingFollowup: permission } }));

    expect(result.replaceBook?.pendingFollowup).toBeNull();
  });
});

describe('deltaReducer', () => {
  it('appends new assistant message on first delta', () => {
    const snap = makeSnapshot();
    const r = deltaReducer({ kind: 'delta', body: { text: 'hello' }, sequence: 1 }, snap);
    expect(r.statusHint).toBe('running');
    expect(r.queueHint).toBe('');
    expect(r.messageUpserts.length).toBe(1);
    expect(r.messageUpserts[0]?.op).toBe('append');
    // 工具前不确定阶段标 process，避免旁白冒充正文触发自动折
    expect(r.messageUpserts[0]?.message?.segmentRole).toBe('process');
    expect(r.replaceBook?.assistantId).not.toBeNull();
    expect(r.replaceBook?.firstChunkAt).toBe(snap.now);
  });

  it('preserves pendingPlan across delta so Plan Board keeps approved body during execution', () => {
    const assistantId = 'm-approved';
    const pendingPlan = {
      plan: '# 已批准方案\n\n1. 写代码',
      planFile: 'plans/owner/sid/plan.md',
      status: 'approved' as const,
    };
    const snap = makeSnapshot({
      messages: [{
        id: assistantId,
        role: 'assistant',
        content: '',
        timestamp: 0,
        streaming: true,
        segmentRole: 'process',
      }],
      book: {
        ...emptyBook(),
        assistantId,
        firstChunkAt: null,
        pendingPlan,
        planActive: false,
      },
    });
    const r = deltaReducer({ kind: 'delta', body: { text: '开始执行' }, sequence: 1 }, snap);
    expect(r.replaceBook?.pendingPlan).toEqual(pendingPlan);
    expect(r.messageUpserts[0]?.op).toBe('patch');
    expect(r.messageUpserts[0]?.patch?.content).toBe('开始执行');
  });

  it('patches existing assistant content on subsequent delta', () => {
    const assistantId = 'm-existing';
    const snap = makeSnapshot({
      messages: [{ id: assistantId, role: 'assistant', content: 'hel', timestamp: 0, streaming: true, segmentRole: 'process' }],
      book: { ...emptyBook(), assistantId, firstChunkAt: 100 },
    });
    const r = deltaReducer({ kind: 'delta', body: { text: 'lo' }, sequence: 2 }, snap);
    expect(r.messageUpserts[0]?.op).toBe('patch');
    expect(r.messageUpserts[0]?.patch?.content).toBe('hello');
  });

  it('starts a new assistant segment after prior tools have settled', () => {
    const assistantId = 'm-round-1';
    const toolMap = new Map();
    toolMap.set('t1', {
      toolCallId: 't1',
      name: 'terminal',
      args: '{}',
      status: 'done' as const,
      startedAt: 100,
      duration: 50,
    });
    const snap = makeSnapshot({
      messages: [{
        id: assistantId,
        role: 'assistant',
        content: '我先查一下。',
        timestamp: 0,
        streaming: true,
        turnStartedAt: 1000,
        toolCalls: [{ toolCallId: 't1', name: 'terminal', status: 'done', startedAt: 100, duration: 50 }],
      }],
      book: { ...emptyBook(), assistantId, toolMap, firstChunkAt: 100 },
      now: 1500,
    });
    const r = deltaReducer({ kind: 'delta', body: { text: '最终' }, sequence: 3 }, snap);
    expect(r.messageUpserts.some((u) => u.op === 'append' && u.message?.content === '最终')).toBe(true);
    // 封存段：停 streaming 且定格耗时（now - turnStartedAt = 500ms → 显示「已处理 0.5s」、不转圈）
    const freeze = r.messageUpserts.find((u) => u.op === 'patch' && u.messageId === assistantId);
    expect(freeze?.patch?.streaming).toBe(false);
    expect(freeze?.patch?.turnDurationMs).toBe(500);
    // 新段继承 turnStartedAt（计时累计、不归零）并继续 streaming
    const newSeg = r.messageUpserts.find((u) => u.op === 'append' && u.message?.content === '最终');
    expect(newSeg?.message?.turnStartedAt).toBe(1000);
    expect(newSeg?.message?.streaming).toBe(true);
    expect(newSeg?.message?.segmentRole).toBe('answer');
    expect(r.messageUpserts.find((u) => u.op === 'patch' && u.messageId === assistantId)?.patch?.segmentRole).toBe('process');
    expect(r.replaceBook?.assistantId).not.toBe(assistantId);
    expect(r.replaceBook?.toolMap.size).toBe(0);
  });

  it('starts a new answer segment after tools settle even without prior narration', () => {
    const assistantId = 'm-tools-only';
    const toolMap = new Map();
    toolMap.set('t1', {
      toolCallId: 't1',
      name: 'terminal',
      args: '{}',
      status: 'done' as const,
      startedAt: 100,
      duration: 50,
    });
    const snap = makeSnapshot({
      messages: [{
        id: assistantId,
        role: 'assistant',
        content: '',
        timestamp: 0,
        streaming: true,
        turnStartedAt: 1000,
        segmentRole: 'process',
        toolCalls: [{ toolCallId: 't1', name: 'terminal', status: 'done', startedAt: 100, duration: 50 }],
      }],
      book: { ...emptyBook(), assistantId, toolMap, firstChunkAt: 100 },
      now: 1500,
    });
    const r = deltaReducer({ kind: 'delta', body: { text: '根据查询结果…' }, sequence: 4 }, snap);
    const newSeg = r.messageUpserts.find((u) => u.op === 'append' && u.message?.content === '根据查询结果…');
    expect(newSeg?.message?.segmentRole).toBe('answer');
    expect(newSeg?.message?.toolCalls).toBeUndefined();
    const freeze = r.messageUpserts.find((u) => u.op === 'patch' && u.messageId === assistantId);
    expect(freeze?.patch?.segmentRole).toBe('process');
    expect(freeze?.patch?.streaming).toBe(false);
    expect(freeze?.patch?.turnDurationMs).toBe(500);
  });

  it('rebuilds assistant content by ordered delta spans', () => {
    const assistantId = 'm-existing';
    const first = deltaReducer(
      { kind: 'delta', body: { text: 'world', delta_start: 2, delta_end: 2 }, sequence: 2 },
      makeSnapshot({
        messages: [{ id: assistantId, role: 'assistant', content: 'world', timestamp: 0, streaming: true }],
        book: {
          ...emptyBook(),
          assistantId,
          firstChunkAt: 100,
          deltaSpans: [{ start: 2, end: 2, text: 'world' }],
        },
      }),
    );
    expect(first.messageUpserts[0]?.patch?.content).toBe('world');

    const second = deltaReducer(
      { kind: 'delta', body: { text: 'hello ', delta_start: 1, delta_end: 1 }, sequence: 1 },
      makeSnapshot({
        messages: [{ id: assistantId, role: 'assistant', content: 'world', timestamp: 0, streaming: true }],
        book: first.replaceBook!,
      }),
    );
    expect(second.messageUpserts[0]?.patch?.content).toBe('hello world');
  });

  it('deduplicates merged delta spans that cover earlier spans', () => {
    const book = { deltaSpans: [], legacyDeltaText: '' };
    expect(applyOrderedDelta(book, { kind: 'delta', body: { text: 'hel', delta_start: 1, delta_end: 1 }, sequence: 1 })).toBe('hel');
    expect(applyOrderedDelta(book, { kind: 'delta', body: { text: 'hello ', delta_start: 1, delta_end: 2 }, sequence: 2 })).toBe('hello ');
    expect(applyOrderedDelta(book, { kind: 'delta', body: { text: 'world', delta_start: 3, delta_end: 3 }, sequence: 3 })).toBe('hello world');
  });

  it('keeps legacy delta frames in arrival order', () => {
    const book = { deltaSpans: [], legacyDeltaText: '' };
    expect(applyOrderedDelta(book, { kind: 'delta', body: { text: 'a' }, sequence: 0 })).toBe('a');
    expect(applyOrderedDelta(book, { kind: 'delta', body: { text: 'b' }, sequence: 0 })).toBe('ab');
  });
});

describe('thinkingReducer', () => {
  it('patches assistant thinking field', () => {
    const assistantId = 'm-1';
    const snap = makeSnapshot({
      messages: [{ id: assistantId, role: 'assistant', content: '', timestamp: 0, streaming: true }],
      book: { ...emptyBook(), assistantId },
    });
    const r = thinkingReducer({ kind: 'thinking', body: { text: 'hmm' }, sequence: 1 }, snap);
    expect(r.messageUpserts[0]?.patch?.thinking).toBe('hmm');
  });

  it('appends streaming ACP thinking chunks', () => {
    const assistantId = 'm-1';
    const snap = makeSnapshot({
      messages: [{
        id: assistantId,
        role: 'assistant',
        content: '',
        timestamp: 0,
        streaming: true,
        thinking: '我先检查',
      }],
      book: { ...emptyBook(), assistantId },
    });
    const r = thinkingReducer({ kind: 'thinking', body: { text: '项目结构' }, sequence: 2 }, snap);
    expect(r.messageUpserts[0]?.patch?.thinking).toBe('我先检查项目结构');
  });

  it('keeps cumulative ACP thinking snapshots without duplication', () => {
    const assistantId = 'm-1';
    const snap = makeSnapshot({
      messages: [{
        id: assistantId,
        role: 'assistant',
        content: '',
        timestamp: 0,
        streaming: true,
        thinking: '我先检查',
      }],
      book: { ...emptyBook(), assistantId },
    });
    const r = thinkingReducer({ kind: 'thinking', body: { text: '我先检查项目结构' }, sequence: 2 }, snap);
    expect(r.messageUpserts[0]?.patch?.thinking).toBe('我先检查项目结构');
  });

  it('is a no-op when no assistantId', () => {
    const snap = makeSnapshot();
    const r = thinkingReducer({ kind: 'thinking', body: { text: 'hmm' }, sequence: 1 }, snap);
    expect(r.messageUpserts.length).toBe(0);
  });

  it('patches thinking on optimistic assistant without resetting turnStartedAt', () => {
    const assistantId = 'm-optimistic';
    const startedAt = 1_000;
    const snap = makeSnapshot({
      now: 5_000,
      messages: [{
        id: assistantId,
        role: 'assistant',
        content: '',
        timestamp: startedAt,
        streaming: true,
        turnStartedAt: startedAt,
      }],
      book: { ...emptyBook(), assistantId, firstChunkAt: null },
    });
    const r = thinkingReducer({ kind: 'thinking', body: { text: '先规划一下' }, sequence: 1 }, snap);
    expect(r.messageUpserts).toHaveLength(1);
    expect(r.messageUpserts[0]?.op).toBe('patch');
    expect(r.messageUpserts[0]?.messageId).toBe(assistantId);
    expect(r.messageUpserts[0]?.patch?.thinking).toBe('先规划一下');
    expect(r.messageUpserts[0]?.patch?.turnStartedAt).toBeUndefined();
    expect(r.replaceBook?.firstChunkAt).toBe(5_000);
    expect(r.replaceBook?.assistantId).toBe(assistantId);
  });
});

describe('toolReducer', () => {
  it('records new tool start', () => {
    const snap = makeSnapshot();
    const r = toolReducer(
      { kind: 'tool', body: { tool_call_id: 't1', phase: 'start', name: 'web_search', args: '{"q":"x"}' }, sequence: 1 },
      snap,
    );
    expect(r.toolUpserts[0]?.status).toBe('running');
    expect(r.replaceBook?.toolMap.get('t1')?.name).toBe('web_search');
  });

  it('keeps generating and start as one desktop tool card', () => {
    const generating = toolReducer(
      {
        kind: 'tool',
        body: {
          tool_call_id: 'w1',
          phase: 'generating',
          name: 'file_write',
          ui_label: '正在写入 /tmp/demo.html',
          args: '{"path":"/tmp/demo.html"}',
        },
        sequence: 1,
      },
      makeSnapshot(),
    );
    const genTool = generating.replaceBook?.toolMap.get('w1');
    expect(genTool?.status).toBe('generating');
    expect(genTool?.uiLabel).toBe('正在写入 /tmp/demo.html');

    const started = toolReducer(
      {
        kind: 'tool',
        body: {
          tool_call_id: 'w1',
          phase: 'start',
          name: 'file_write',
          ui_label: '写入 /tmp/demo.html',
          args: '{"path":"/tmp/demo.html","content":"ok"}',
        },
        sequence: 2,
      },
      makeSnapshot({ book: generating.replaceBook, now: 1_700_000_000_200 }),
    );
    const startedTool = started.replaceBook?.toolMap.get('w1');
    expect(started.replaceBook?.toolMap.size).toBe(1);
    expect(startedTool?.status).toBe('running');
    expect(startedTool?.startedAt).toBe(1_700_000_000_000);
    expect(startedTool?.uiLabel).toBe('写入 /tmp/demo.html');
  });

  it('marks tool done on end phase', () => {
    const toolMap = new Map();
    toolMap.set('t1', {
      toolCallId: 't1', name: 'web_search', args: '{}', status: 'running' as const, startedAt: 100,
    });
    const snap = makeSnapshot({ book: { ...emptyBook(), toolMap } });
    const r = toolReducer(
      { kind: 'tool', body: { tool_call_id: 't1', phase: 'end', detail: 'done' }, sequence: 2 },
      { ...snap, now: 200 },
    );
    const updated = r.replaceBook?.toolMap.get('t1');
    expect(updated?.status).toBe('done');
    expect(updated?.duration).toBe(100);
  });

  it('marks tool error on error phase', () => {
    const toolMap = new Map();
    toolMap.set('t1', { toolCallId: 't1', name: 'web_search', args: '{}', status: 'running' as const, startedAt: 100 });
    const snap = makeSnapshot({ book: { ...emptyBook(), toolMap } });
    const r = toolReducer(
      { kind: 'tool', body: { tool_call_id: 't1', phase: 'error', detail: 'fail' }, sequence: 2 },
      { ...snap, now: 150 },
    );
    expect(r.replaceBook?.toolMap.get('t1')?.status).toBe('error');
  });
});

describe('statusReducer', () => {
  it('sets queued hint when message includes 排队', () => {
    const r = statusReducer({ kind: 'status', body: { message: '排队中...' }, sequence: 1 }, makeSnapshot());
    expect(r.statusHint).toBe('queued');
    expect(r.queueHint).toBe('排队中...');
  });

  it('opens a streaming anchor + appends status when no active turn (per-turn live signal)', () => {
    // 动态看板等只来 status 帧的回合也需要 per-turn live 信号：首片 status 开一条 streaming
    // anchor，renderAgentTurn 据此显示「执行中」，不再借用 session 全局 busy。
    const r = statusReducer({ kind: 'status', body: { message: 'thinking...' }, sequence: 1 }, makeSnapshot());
    expect(r.statusHint).toBe('running');
    expect(r.messageUpserts[0]?.message?.role).toBe('assistant');
    expect(r.messageUpserts[0]?.message?.streaming).toBe(true);
    expect(r.messageUpserts[1]?.message?.role).toBe('status');
    expect(r.replaceBook?.assistantId).toBe(r.messageUpserts[0]?.message?.id);
  });

  it('does not open an anchor when turn already sealed (late status after finalize)', () => {
    // status 帧不被 gate 在封口后丢弃，reducer 必须自行用 !turnSealed 守卫，避免开幽灵回合。
    const r = statusReducer(
      { kind: 'status', body: { message: 'thinking...' }, sequence: 1 },
      makeSnapshot({ book: { ...emptyBook(), turnSealed: true } }),
    );
    expect(r.messageUpserts.every((u) => u.message?.role !== 'assistant')).toBe(true);
    expect(r.replaceBook).toBeNull();
    expect(r.messageUpserts[0]?.message?.role).toBe('status');
  });

  it('does not open a second anchor when one already exists', () => {
    const r = statusReducer(
      { kind: 'status', body: { message: 'thinking...' }, sequence: 1 },
      makeSnapshot({ book: { ...emptyBook(), assistantId: 'm-existing' } }),
    );
    expect(r.messageUpserts.some((u) => u.message?.role === 'assistant')).toBe(false);
    expect(r.replaceBook).toBeNull();
    expect(r.messageUpserts[0]?.message?.role).toBe('status');
  });

  it('handles plan control status without appending a message', () => {
    const r = statusReducer(
      { kind: 'status', body: { message: '已进入 Plan 模式（只读探索→写计划→审批后执行）' }, sequence: 1 },
      makeSnapshot(),
    );
    expect(r.messageUpserts).toEqual([]);
    expect(r.statusHint).toBe('idle');
    expect(r.queueHint).toBe('');
    expect(r.replaceBook?.planActive).toBe(true);
  });

  it('keeps existing plan and marks editing on plan reject status', () => {
    const r = statusReducer(
      { kind: 'status', body: { message: '已保留 Plan 模式，请继续完善计划' }, sequence: 1 },
      makeSnapshot({
        book: {
          ...emptyBook(),
          planActive: true,
          pendingPlan: { plan: 'old plan', planFile: 'plans/p.md', status: 'pending' },
        },
      }),
    );
    expect(r.messageUpserts).toEqual([]);
    expect(r.replaceBook?.planActive).toBe(true);
    expect(r.replaceBook?.pendingPlan).toEqual({ plan: 'old plan', planFile: 'plans/p.md', status: 'editing' });
  });

  it('clears plan state on plan exit status', () => {
    const r = statusReducer(
      { kind: 'status', body: { message: '已退出 Plan 模式' }, sequence: 1 },
      makeSnapshot({
        book: {
          ...emptyBook(),
          planActive: true,
          pendingPlan: { plan: 'old plan', planFile: 'plans/p.md', status: 'pending' },
        },
      }),
    );
    expect(r.messageUpserts).toEqual([]);
    expect(r.replaceBook?.planActive).toBe(false);
    expect(r.replaceBook?.pendingPlan).toBeNull();
  });

  it('control status appends without driving running or queue state', () => {
    const r = statusReducer({ kind: 'status', body: { message: '已停止', control: true }, sequence: 1 }, makeSnapshot());
    expect(r.statusHint).toBeUndefined();
    expect(r.queueHint).toBeUndefined();
    expect(r.messageUpserts[0]?.message?.role).toBe('status');
  });
});

describe('finalReducer', () => {
  it('promotes process segment to answer on final when no tools ran', () => {
    const assistantId = 'm-plain';
    const messages: ChatMessage[] = [{
      id: assistantId,
      role: 'assistant',
      content: '纯文字回复',
      timestamp: 1000,
      streaming: true,
      turnStartedAt: 1000,
      segmentRole: 'process',
    }];
    const snap = makeSnapshot({ messages, book: { ...emptyBook(), assistantId, firstChunkAt: 1100 } });
    const r = finalReducer({ kind: 'final', body: { text: '纯文字回复' }, sequence: 99 }, { ...snap, now: 1500 });
    const patch = r.messageUpserts.find((u) => u.op === 'patch' && u.messageId === assistantId);
    expect(patch?.patch?.segmentRole).toBe('answer');
    expect(patch?.patch?.streaming).toBe(false);
  });

  it('keeps process when final arrives on a tool-bearing segment', () => {
    const assistantId = 'm-with-tools';
    const toolMap = new Map();
    toolMap.set('t1', {
      toolCallId: 't1',
      name: 'terminal',
      status: 'done' as const,
      startedAt: 100,
      duration: 50,
    });
    const messages: ChatMessage[] = [{
      id: assistantId,
      role: 'assistant',
      content: '我先查一下。',
      timestamp: 1000,
      streaming: true,
      turnStartedAt: 1000,
      segmentRole: 'process',
      toolCalls: [{ toolCallId: 't1', name: 'terminal', status: 'done', startedAt: 100, duration: 50 }],
    }];
    const snap = makeSnapshot({
      messages,
      book: { ...emptyBook(), assistantId, toolMap, firstChunkAt: 1100 },
    });
    const r = finalReducer({ kind: 'final', body: { text: '' }, sequence: 99 }, { ...snap, now: 1500 });
    const patches = r.messageUpserts.filter((u) => u.op === 'patch' && u.messageId === assistantId);
    const lastRole = [...patches].reverse().find((u) => u.patch?.segmentRole)?.patch?.segmentRole;
    expect(lastRole).toBe('process');
  });

  it('patches assistant content and marks finalize', () => {
    const assistantId = 'm-1';
    const startedAt = 1000;
    const messages: ChatMessage[] = [{ id: assistantId, role: 'assistant', content: 'hel', timestamp: startedAt, streaming: true, turnStartedAt: startedAt }];
    const snap = makeSnapshot({ messages, book: { ...emptyBook(), assistantId, firstChunkAt: 1100 } });
    const r = finalReducer({ kind: 'final', body: { text: 'hello' }, sequence: 99 }, { ...snap, now: 1500 });
    expect(r.finalize).toBe(true);
    expect(r.turn?.status).toBe(200);
    expect(r.turn?.firstTokenMs).toBe(100);
    expect(r.turn?.turnDurationMs).toBe(500);
    const patch = r.messageUpserts.find((u) => u.op === 'patch' && u.messageId === assistantId);
    expect(patch && patch.op === 'patch' ? patch.patch.timestamp : undefined).toBe(1500);
  });

  it('appends assistant message if none existed', () => {
    const snap = makeSnapshot();
    const r = finalReducer({ kind: 'final', body: { text: 'direct answer' }, sequence: 1 }, snap);
    expect(r.messageUpserts.some((u) => u.op === 'append')).toBe(true);
  });

  it('treats root final as lifecycle-only after team_internal and removes the optimistic assistant', () => {
    const assistantId = 'team-root-anchor';
    const messages: ChatMessage[] = [
      { id: 'user-1', role: 'user', content: '开发贪吃蛇', timestamp: 900 },
      { id: assistantId, role: 'assistant', content: '', timestamp: 1000, streaming: true, turnStartedAt: 1000 },
      {
        id: 'team-plan',
        role: 'team_internal',
        content: '收到，我会按团队流程推进。',
        timestamp: 1100,
        agentId: 'crew::builtin',
        eventType: 'team_decision',
      },
    ];
    const snap = makeSnapshot({
      messages,
      book: { ...emptyBook(), assistantId, hadTeamInternal: true },
    });

    const result = finalReducer(
      { kind: 'final', body: { text: '最终总结' }, sequence: 99 },
      { ...snap, now: 1500 },
    );

    expect(result.messageUpserts).toEqual([{ op: 'remove', messageId: assistantId }]);
    expect(result.replaceBook?.assistantId).toBeNull();
    expect(result.messageUpserts.some((upsert) => upsert.op === 'append')).toBe(false);
  });

  it('keeps accumulated multi-turn content when final is only the last turn', () => {
    // 流式累积了「报告正文 + 总结」，final 只带最后一轮「总结」——不能覆盖丢正文。
    const assistantId = 'm-1';
    const accumulated = '调研报告正文……\n\n以上就是总结';
    const messages: ChatMessage[] = [{ id: assistantId, role: 'assistant', content: accumulated, timestamp: 1000, streaming: true, turnStartedAt: 1000 }];
    const snap = makeSnapshot({ messages, book: { ...emptyBook(), assistantId, firstChunkAt: 1100 } });
    const r = finalReducer({ kind: 'final', body: { text: '以上就是总结' }, sequence: 99 }, { ...snap, now: 1500 });
    const patch = r.messageUpserts.find((u) => u.op === 'patch' && u.messageId === assistantId);
    expect(patch && patch.op === 'patch' ? patch.patch.content : undefined).toBeUndefined();
    expect(patch && patch.op === 'patch' ? patch.patch.streaming : undefined).toBe(false);
  });

  it('overwrites when accumulated is a prefix of final (tail-frame loss recovery)', () => {
    // 单段回合丢了尾帧：acc='完整最终'（final 的前缀），final='完整最终答案'（超集）→ 覆盖只补全。
    const assistantId = 'm-1';
    const messages: ChatMessage[] = [{ id: assistantId, role: 'assistant', content: '完整最终', timestamp: 1000, streaming: true, turnStartedAt: 1000 }];
    const snap = makeSnapshot({ messages, book: { ...emptyBook(), assistantId, firstChunkAt: 1100 } });
    const r = finalReducer({ kind: 'final', body: { text: '完整最终答案' }, sequence: 99 }, { ...snap, now: 1500 });
    const patch = r.messageUpserts.find((u) => u.op === 'patch' && u.messageId === assistantId);
    expect(patch && patch.op === 'patch' ? patch.patch.content : undefined).toBe('完整最终答案');
  });

  it('keeps accumulated when final is not a superset (multi-step last-segment / mid loss)', () => {
    // builtin executor 多步回合：final 只含末段；acc 含前言。final 不是 acc 的超集 →
    // 绝不覆盖（否则丢前言）。真缺口留给重连 history 回填。
    const assistantId = 'm-1';
    const messages: ChatMessage[] = [{ id: assistantId, role: 'assistant', content: '前言正文……\n\n半截', timestamp: 1000, streaming: true, turnStartedAt: 1000 }];
    const snap = makeSnapshot({ messages, book: { ...emptyBook(), assistantId, firstChunkAt: 1100 } });
    const r = finalReducer({ kind: 'final', body: { text: '半截' }, sequence: 99 }, { ...snap, now: 1500 });
    const patch = r.messageUpserts.find((u) => u.op === 'patch' && u.messageId === assistantId);
    expect(patch && patch.op === 'patch' ? patch.patch.content : undefined).toBeUndefined();
    expect(patch && patch.op === 'patch' ? patch.patch.streaming : undefined).toBe(false);
  });

  it('uses final content when accumulated text is likely reordered', () => {
    expect(resolveFinalContent('lohel', 'hello')).toBe('hello');
  });
});

describe('errorReducer', () => {
  it('appends error message and marks finalize with status 500', () => {
    const assistantId = 'm-1';
    const messages: ChatMessage[] = [{ id: assistantId, role: 'assistant', content: 'part', timestamp: 100, streaming: true, turnStartedAt: 100 }];
    const snap = makeSnapshot({ messages, book: { ...emptyBook(), assistantId, firstChunkAt: 110 } });
    const r = errorReducer({ kind: 'error', body: { message: 'oops' }, sequence: 5 }, { ...snap, now: 200 });
    expect(r.finalize).toBe(true);
    expect(r.statusHint).toBe('error');
    expect(r.turn?.status).toBe(500);
    const patch = r.messageUpserts.find((u) => u.op === 'patch' && u.messageId === assistantId);
    expect(patch && patch.op === 'patch' ? patch.patch.timestamp : undefined).toBe(200);
    expect(patch && patch.op === 'patch' ? patch.patch.streaming : undefined).toBe(false);
    expect(r.messageUpserts.some((u) => u.op === 'append' && u.message?.role === 'error')).toBe(true);
  });
});

describe('planReviewReducer', () => {
  it('stores plan + planFile in book.pendingPlan', () => {
    const snap = makeSnapshot();
    const r = planReviewReducer(
      { kind: 'plan_review', body: { plan: 'step 1...', plan_file: '/tmp/p.md' }, sequence: 1 },
      snap,
    );
    expect(r.replaceBook?.pendingPlan).toEqual({ plan: 'step 1...', planFile: '/tmp/p.md', status: 'pending' });
    expect(r.replaceBook?.planActive).toBe(true);
    expect(r.statusHint).toBe('idle');
  });
});

describe('todoUpdatedReducer', () => {
  it('stores todos in book.todos and patches assistant message todoSnapshot', () => {
    const assistantId = 'm-1';
    const messages: ChatMessage[] = [
      { id: assistantId, role: 'assistant', content: '', timestamp: 100, streaming: true },
    ];
    const snap = makeSnapshot({ messages, book: { ...emptyBook(), assistantId } });
    const todos = [
      { id: '1', content: 'A', status: 'pending' as const },
      { id: '2', content: 'B', status: 'in_progress' as const },
    ];
    const r = todoUpdatedReducer(
      { kind: 'todo_updated', body: { todos }, sequence: 1 },
      snap,
    );
    expect(r.replaceBook?.todos).toEqual(todos);
    expect(r.messageUpserts).toHaveLength(1);
    const patch = r.messageUpserts[0];
    expect(patch.op).toBe('patch');
    expect(patch.messageId).toBe(assistantId);
    expect(patch.patch?.todoSnapshot).toEqual(todos);
  });
});

describe('workflowProgressReducer', () => {
  it('appends a new workflow progress message on first chunk', () => {
    const snap = makeSnapshot();
    const chunk = {
      kind: 'workflow_progress' as const,
      body: {
        workflow_id: 'wf-1',
        status: 'running',
        current_phase: { id: 'p1', name: '多源搜集', status: 'running' },
        completed_phases: [],
        active_calls: [{ call_id: 'c1', role: 'analyst' }],
        message: '进入阶段 多源搜集',
      },
      sequence: 1,
    };
    const r = workflowProgressReducer(chunk, snap);
    expect(r.messageUpserts).toHaveLength(1);
    const upsert = r.messageUpserts[0];
    expect(upsert.op).toBe('append');
    expect(upsert.message?.id).toBe('wp-wf-1');
    expect(upsert.message?.role).toBe('status');
    expect(upsert.message?.workflowProgress).toEqual({
      workflow_id: 'wf-1',
      status: 'running',
      current_phase: { id: 'p1', name: '多源搜集', status: 'running', description: '' },
      completed_phases: [],
      active_calls: [{ call_id: 'c1', role: 'analyst' }],
      message: '进入阶段 多源搜集',
    });
    expect(r.statusHint).toBe('running');
  });

  it('patches existing progress message when workflow_id matches', () => {
    const existing: ChatMessage = {
      id: 'wp-wf-1',
      role: 'status',
      content: '',
      timestamp: 100,
      workflowProgress: {
        workflow_id: 'wf-1',
        status: 'running',
        current_phase: { id: 'p1', name: '多源搜集', status: 'running', description: '' },
        completed_phases: [],
        active_calls: [{ call_id: 'c1', role: 'analyst' }],
      },
    };
    const snap = makeSnapshot({ messages: [existing] });
    const chunk = {
      kind: 'workflow_progress' as const,
      body: {
        workflow_id: 'wf-1',
        status: 'running',
        current_phase: { id: 'p2', name: '综合研究', status: 'running' },
        completed_phases: [{ id: 'p1', name: '多源搜集', status: 'done' }],
        active_calls: [{ call_id: 'c2', role: 'writer' }],
        message: '阶段推进',
      },
      sequence: 2,
    };
    const r = workflowProgressReducer(chunk, snap);
    expect(r.messageUpserts).toHaveLength(1);
    const upsert = r.messageUpserts[0];
    expect(upsert.op).toBe('patch');
    expect(upsert.messageId).toBe('wp-wf-1');
    expect(upsert.patch?.workflowProgress?.current_phase?.id).toBe('p2');
    expect(upsert.patch?.workflowProgress?.completed_phases).toHaveLength(1);
  });

  it('drops chunk with missing workflow_id', () => {
    const snap = makeSnapshot();
    const r = workflowProgressReducer(
      { kind: 'workflow_progress', body: { workflow_id: '' }, sequence: 1 },
      snap,
    );
    expect(r.messageUpserts).toEqual([]);
  });

  it('done status releases busy and promotes last agent role output as final answer', () => {
    const roleOutput: ChatMessage = {
      id: 'role-1',
      role: 'status',
      content: '这是 writer 的最终报告。',
      timestamp: 100,
      agentName: 'writer',
      agentAvatar: '📝',
    };
    const snap = makeSnapshot({ messages: [roleOutput] });
    const chunk = {
      kind: 'workflow_progress' as const,
      body: {
        workflow_id: 'wf-1',
        status: 'done',
        message: 'Workflow 已完成',
      },
      sequence: 2,
    };
    const r = workflowProgressReducer(chunk, snap);
    expect(r.statusHint).toBe('idle');
    expect(r.replaceBook?.assistantId).toBe('role-1');
    expect(r.messageUpserts).toHaveLength(2);
    const promote = r.messageUpserts.find((u) => u.op === 'patch' && u.messageId === 'role-1');
    expect(promote?.patch).toMatchObject({ role: 'assistant', segmentRole: 'answer' });
  });

  it('failed status releases busy with error hint', () => {
    const snap = makeSnapshot();
    const chunk = {
      kind: 'workflow_progress' as const,
      body: { workflow_id: 'wf-1', status: 'failed', message: 'Workflow 失败' },
      sequence: 1,
    };
    const r = workflowProgressReducer(chunk, snap);
    expect(r.statusHint).toBe('error');
  });
});

describe('reduceChunk dispatch', () => {
  it('routes each kind to its reducer', () => {
    const snap = makeSnapshot();
    expect(reduceChunk({ kind: 'delta', body: {}, sequence: 1 }, snap).statusHint).toBe('running');
    expect(reduceChunk({ kind: 'thinking', body: {}, sequence: 1 }, snap).statusHint).toBe('running');
    expect(reduceChunk({ kind: 'tool', body: {}, sequence: 1 }, snap).statusHint).toBe('running');
    expect(reduceChunk({ kind: 'status', body: { message: '排队中' }, sequence: 1 }, snap).statusHint).toBe('queued');
    expect(reduceChunk({ kind: 'final', body: {}, sequence: 1 }, snap).finalize).toBe(true);
    expect(reduceChunk({ kind: 'error', body: {}, sequence: 1 }, snap).finalize).toBe(true);
    expect(reduceChunk({ kind: 'plan_review', body: {}, sequence: 1 }, snap).statusHint).toBe('idle');
    expect(reduceChunk({ kind: 'workflow_progress', body: { workflow_id: 'wf-1' }, sequence: 1 }, snap).statusHint).toBe('running');
  });

  it('unknown kind returns empty patch (no throw)', () => {
    const r = reduceChunk(
      // 强制走 default 分支
      { kind: '__unknown_kind__', body: {}, sequence: 1 } as never,
      makeSnapshot(),
    );
    expect(r.messageUpserts).toEqual([]);
    expect(r.finalize).toBe(false);
  });
});

// 构造一个轻量 FileChange（带最小 diff 行，使签名哈希稳定）。
function fc(path: string, added: number, removed: number, status: FileChange['status'] = 'modified', diffText = 'x'): FileChange {
  return {
    path,
    name: path.split(/[\\/]/).pop() || path,
    added,
    removed,
    status,
    diff: [{ line: 1, kind: 'add', text: diffText }],
  };
}

// 从 final 结果里取出 patch 到目标消息的 turnFileChanges（final 会发多条 patch，只取带该字段的那条）。
function turnFilesOf(r: ReturnType<typeof finalReducer>, id: string) {
  const u = r.messageUpserts.find(
    (p): p is Extract<typeof p, { op: 'patch' }> => p.op === 'patch' && p.messageId === id && !!p.patch && 'turnFileChanges' in p.patch,
  );
  return u?.patch?.turnFileChanges;
}

describe('finalReducer per-turn file changes', () => {
  it('emits all current files on the first turn (baseline empty)', () => {
    const id = 'm-1';
    const messages: ChatMessage[] = [{ id, role: 'assistant', content: 'hi', timestamp: 1000, streaming: true, turnStartedAt: 1000 }];
    const snap = makeSnapshot({ messages, book: { ...emptyBook(), assistantId: id, fileChanges: [fc('crew/a.py', 3, 1)] } });
    const r = finalReducer({ kind: 'final', body: { text: 'hi' }, sequence: 1 }, { ...snap, now: 1500 });
    expect(turnFilesOf(r, id)).toEqual([{ path: 'crew/a.py', name: 'a.py', added: 3, removed: 1, status: 'modified' }]);
    // 基准前进到当前快照，供下一轮差集
    expect(r.replaceBook?.prevTurnFileSignature).not.toBeNull();
  });

  it('turn 2 only reports files changed since turn 1 (cumulative snapshot)', () => {
    // 轮 1：改 a.py
    const id1 = 'm-1';
    const fileA = fc('crew/a.py', 3, 1);
    const snap1 = makeSnapshot({
      messages: [{ id: id1, role: 'assistant', content: 'hi', timestamp: 1000, streaming: true, turnStartedAt: 1000 }],
      book: { ...emptyBook(), assistantId: id1, fileChanges: [fileA] },
    });
    const r1 = finalReducer({ kind: 'final', body: { text: 'hi' }, sequence: 1 }, { ...snap1, now: 1500 });
    expect(turnFilesOf(r1, id1)).toHaveLength(1);

    // 轮 2：后端累计快照含 a.py（未变）+ b.py（本轮新增）
    const id2 = 'm-2';
    const fileB = fc('crew/b.py', 9, 0);
    const snap2 = makeSnapshot({
      messages: [{ id: id2, role: 'assistant', content: 'yo', timestamp: 2000, streaming: true, turnStartedAt: 2000 }],
      book: { ...r1.replaceBook!, assistantId: id2, firstChunkAt: 2100, fileChanges: [fileA, fileB] },
    });
    const r2 = finalReducer({ kind: 'final', body: { text: 'yo' }, sequence: 2 }, { ...snap2, now: 2500 });
    expect(turnFilesOf(r2, id2)).toEqual([{ path: 'crew/b.py', name: 'b.py', added: 9, removed: 0, status: 'modified' }]);
  });

  it('includes a file edited again this turn (signature changed)', () => {
    const id1 = 'm-1';
    const fileA1 = fc('crew/a.py', 3, 1);
    const snap1 = makeSnapshot({
      messages: [{ id: id1, role: 'assistant', content: 'hi', timestamp: 1000, streaming: true, turnStartedAt: 1000 }],
      book: { ...emptyBook(), assistantId: id1, fileChanges: [fileA1] },
    });
    const r1 = finalReducer({ kind: 'final', body: { text: 'hi' }, sequence: 1 }, { ...snap1, now: 1500 });

    // 轮 2：a.py 再次被改（增删计数变了 → 签名变）
    const id2 = 'm-2';
    const fileA2 = fc('crew/a.py', 5, 2);
    const snap2 = makeSnapshot({
      messages: [{ id: id2, role: 'assistant', content: 'yo', timestamp: 2000, streaming: true, turnStartedAt: 2000 }],
      book: { ...r1.replaceBook!, assistantId: id2, firstChunkAt: 2100, fileChanges: [fileA2] },
    });
    const r2 = finalReducer({ kind: 'final', body: { text: 'yo' }, sequence: 2 }, { ...snap2, now: 2500 });
    expect(turnFilesOf(r2, id2)).toEqual([{ path: 'crew/a.py', name: 'a.py', added: 5, removed: 2, status: 'modified' }]);
  });

  it('includes a metadata-only edit when revision changes but line counts stay equal', () => {
    const id1 = 'm-revision-1';
    const fileA1 = { ...fc('crew/a.py', 0, 0, 'modified', ''), revision: '100:20' };
    const r1 = finalReducer(
      { kind: 'final', body: { text: 'first' }, sequence: 1 },
      makeSnapshot({
        messages: [{ id: id1, role: 'assistant', content: 'first', timestamp: 1000, streaming: true }],
        book: { ...emptyBook(), assistantId: id1, fileChanges: [fileA1] },
      }),
    );

    const id2 = 'm-revision-2';
    const fileA2 = { ...fileA1, revision: '200:20' };
    const r2 = finalReducer(
      { kind: 'final', body: { text: 'second' }, sequence: 2 },
      makeSnapshot({
        messages: [{ id: id2, role: 'assistant', content: 'second', timestamp: 2000, streaming: true }],
        book: { ...r1.replaceBook!, assistantId: id2, fileChanges: [fileA2] },
      }),
    );

    expect(turnFilesOf(r2, id2)).toEqual([
      { path: 'crew/a.py', name: 'a.py', added: 0, removed: 0, status: 'modified' },
    ]);
  });

  it('emits no turnFileChanges patch when nothing changed this turn', () => {
    const id1 = 'm-1';
    const fileA = fc('crew/a.py', 3, 1);
    const snap1 = makeSnapshot({
      messages: [{ id: id1, role: 'assistant', content: 'hi', timestamp: 1000, streaming: true, turnStartedAt: 1000 }],
      book: { ...emptyBook(), assistantId: id1, fileChanges: [fileA] },
    });
    const r1 = finalReducer({ kind: 'final', body: { text: 'hi' }, sequence: 1 }, { ...snap1, now: 1500 });

    // 轮 2：fileChanges 与轮 1 完全相同 → 无改动卡
    const id2 = 'm-2';
    const snap2 = makeSnapshot({
      messages: [{ id: id2, role: 'assistant', content: 'yo', timestamp: 2000, streaming: true, turnStartedAt: 2000 }],
      book: { ...r1.replaceBook!, assistantId: id2, firstChunkAt: 2100, fileChanges: [fileA] },
    });
    const r2 = finalReducer({ kind: 'final', body: { text: 'yo' }, sequence: 2 }, { ...snap2, now: 2500 });
    expect(turnFilesOf(r2, id2)).toBeUndefined();
  });

  it('drops empty added ghost entries from turnFileChanges', () => {
    const id = 'm-ghost';
    const ghost = fc('tmp/_smoke.js', 0, 0, 'added');
    const snap = makeSnapshot({
      messages: [{ id, role: 'assistant', content: 'hi', timestamp: 1000, streaming: true, turnStartedAt: 1000 }],
      book: { ...emptyBook(), assistantId: id, fileChanges: [ghost] },
    });
    const r = finalReducer({ kind: 'final', body: { text: 'hi' }, sequence: 1 }, { ...snap, now: 1500 });
    expect(turnFilesOf(r, id)).toBeUndefined();
  });

  it('keeps zero-line binary results in the same turnFileChanges list', () => {
    const id = 'm-bin';
    const result: FileChange = {
      ...fc('output/final.pptx', 0, 0, 'added', ''),
      binary: true,
    };
    const snap = makeSnapshot({
      messages: [{ id, role: 'assistant', content: 'done', timestamp: 1000, streaming: true, turnStartedAt: 1000 }],
      book: { ...emptyBook(), assistantId: id, fileChanges: [result] },
    });
    const r = finalReducer({ kind: 'final', body: { text: 'done' }, sequence: 1 }, { ...snap, now: 1500 });
    expect(turnFilesOf(r, id)).toEqual([{
      path: 'output/final.pptx',
      name: 'final.pptx',
      added: 0,
      removed: 0,
      status: 'added',
      binary: true,
    }]);
  });

  it('keeps deleted status in turnFileChanges after reconcile-style update', () => {
    const id = 'm-del';
    const gone = fc('crew/old.py', 0, 4, 'deleted');
    const snap = makeSnapshot({
      messages: [{ id, role: 'assistant', content: 'hi', timestamp: 1000, streaming: true, turnStartedAt: 1000 }],
      book: { ...emptyBook(), assistantId: id, fileChanges: [gone] },
    });
    const r = finalReducer({ kind: 'final', body: { text: 'hi' }, sequence: 1 }, { ...snap, now: 1500 });
    expect(turnFilesOf(r, id)).toEqual([
      { path: 'crew/old.py', name: 'old.py', added: 0, removed: 4, status: 'deleted' },
    ]);
  });
});
