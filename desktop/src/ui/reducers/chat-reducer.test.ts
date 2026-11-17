import { describe, expect, it } from 'vitest';
import type { Bookkeeping } from '../state';
import { toolReducer, type ToolChunk } from './chat-reducer';

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

  describe('terminal progress', () => {
    function progress(book: Bookkeeping, text: string, offset?: number, gap = false) {
      const body: ToolChunk['body'] = { name: 'terminal', phase: 'progress', tool_call_id: 't1', text };
      if (offset != null) body.progress_metadata = {
        offset,
        next_offset: offset + new TextEncoder().encode(text).length,
        gap,
      };
      return toolReducer({ kind: 'tool', body, sequence: 2 }, snapshot(book, 1100));
    }

    it('preserves bytes and whitespace, skips retransmissions, and appends only the unseen part of an overlap', () => {
      const marker = ' 雪𠮷\n';
      const byteLength = new TextEncoder().encode(marker).length;
      let book = progress(emptyBook(), marker, 0).replaceBook!;
      expect(progress(book, marker, 0).replaceBook).toBeNull();
      book = progress(book, marker + marker, 0).replaceBook!;
      expect(book.toolMap.get('t1')?.terminalProgress).toEqual({
        text: marker + marker,
        clipped: false,
        nextOffset: byteLength * 2,
      });
      expect(book.toolMap.get('t1')?.progressHistory).toBeUndefined();
    });

    it('does not join separated output across a missing range, and clears the preview on a result', () => {
      let book = progress(emptyBook(), 'first', 0).replaceBook!;
      book = progress(book, '新行', 600, true).replaceBook!;
      expect(book.toolMap.get('t1')?.terminalProgress).toEqual({ text: '新行', nextOffset: 606, clipped: true });
      book = toolReducer({
        kind: 'tool', body: { tool_call_id: 't1', phase: 'result', detail: 'done' }, sequence: 3,
      }, snapshot(book, 1200)).replaceBook!;
      expect(book.toolMap.get('t1')).toMatchObject({ status: 'done', result: 'done' });
      expect(book.toolMap.get('t1')?.terminalProgress).toBeUndefined();
      expect(progress(book, 'late', 606).replaceBook).toBeNull();
    });

    it('bounds legacy progress at a Unicode code point boundary and retains valid repeat text', () => {
      const tail = 'a'.repeat(4095);
      let book = progress(emptyBook(), 'x😀' + tail).replaceBook!;
      expect(book.toolMap.get('t1')?.terminalProgress).toEqual({ text: tail, clipped: true });
      book = progress(book, ' 尾\n').replaceBook!;
      book = progress(book, ' 尾\n').replaceBook!;
      const output = book.toolMap.get('t1')!.terminalProgress!;
      expect(output.text.endsWith(' 尾\n 尾\n')).toBe(true);
      expect(output.text.length).toBeLessThanOrEqual(4096);
    });
  });
});
