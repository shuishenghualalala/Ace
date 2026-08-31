import { describe, expect, it } from 'vitest';
import type { Bookkeeping } from '../state';
import { toolReducer } from './chat-reducer';

function emptyBook(): Bookkeeping {
  return {
    toolMap: new Map(),
    assistantId: null,
    hadTeamInternal: false,
    firstChunkAt: null,
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

function snapshot(book: Bookkeeping, now: number) {
  return {
    sessionId: 's1',
    messages: [],
    book,
    currentStatus: 'idle' as const,
    now,
    sequence: 1,
  };
}

describe('toolReducer', () => {
  it('generating 阶段创建 generating 状态条目', () => {
    const book = emptyBook();
    const result = toolReducer(
      { kind: 'tool', body: { tool_call_id: 't1', phase: 'generating', name: 'file_write', args: '{"path":"a.txt"}' }, sequence: 1 },
      snapshot(book, 1000),
    );
    const t = result.replaceBook!.toolMap.get('t1')!;
    expect(t.status).toBe('generating');
    expect(t.startedAt).toBe(1000);
  });

  it('generating → start 保留 startedAt 并切为 running', () => {
    const book = emptyBook();
    let result = toolReducer(
      { kind: 'tool', body: { tool_call_id: 't1', phase: 'generating' }, sequence: 1 },
      snapshot(book, 1000),
    );
    result = toolReducer(
      { kind: 'tool', body: { tool_call_id: 't1', phase: 'start', name: 'file_write' }, sequence: 2 },
      snapshot(result.replaceBook!, 1500),
    );
    const t = result.replaceBook!.toolMap.get('t1')!;
    expect(t.status).toBe('running');
    expect(t.startedAt).toBe(1000);
  });

  it('start → result 计算完整 duration', () => {
    const book = emptyBook();
    let result = toolReducer(
      { kind: 'tool', body: { tool_call_id: 't1', phase: 'start', name: 'file_write' }, sequence: 1 },
      snapshot(book, 1000),
    );
    result = toolReducer(
      { kind: 'tool', body: { tool_call_id: 't1', phase: 'result', detail: 'ok' }, sequence: 2 },
      snapshot(result.replaceBook!, 2500),
    );
    const t = result.replaceBook!.toolMap.get('t1')!;
    expect(t.status).toBe('done');
    expect(t.duration).toBe(1500);
  });

  it('error 阶段状态为 error', () => {
    const book = emptyBook();
    let result = toolReducer(
      { kind: 'tool', body: { tool_call_id: 't1', phase: 'start' }, sequence: 1 },
      snapshot(book, 1000),
    );
    result = toolReducer(
      { kind: 'tool', body: { tool_call_id: 't1', phase: 'error', detail: 'fail' }, sequence: 2 },
      snapshot(result.replaceBook!, 1200),
    );
    const t = result.replaceBook!.toolMap.get('t1')!;
    expect(t.status).toBe('error');
    expect(t.duration).toBe(200);
  });
});
