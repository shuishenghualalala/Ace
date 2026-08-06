/**
 * applyChunk dispatch 适配层单测。
 * 目标：applyChunk 应退化为「normalize → reduce → apply patch → side effects」
 * 薄适配层，所有 7 个 kind 的状态迁移由 chat-reducer 提供。
 */
import { describe, it, expect, beforeEach } from 'vitest';
import {
  reduceChunk,
  normalizeChunk,
  resolveBusyTransition,
  resolveTurnGate,
  type AnyChatChunk,
  type ReducerSnapshot,
} from '../../src/ui/reducers/chat-reducer';
import { __resetAllStoresForTest, messageStore } from '../../src/ui/stores/stores';
import type { Bookkeeping } from '../../src/ui/state';

function snap(overrides: Partial<ReducerSnapshot> = {}): ReducerSnapshot {
  return {
    sessionId: 'sid-1',
    messages: [],
    book: {
      toolMap: new Map(),
      assistantId: null,
      firstChunkAt: null,
      hadTeamInternal: false,
      planActive: false,
      pendingPlan: null,
      pendingFollowup: null,
      todos: [],
      fileChanges: [],
      deltaSpans: [],
      legacyDeltaText: '',
      turnSealed: false,
      activeRequestId: null,
      acceptingNewRequest: false,
    } as Bookkeeping,
    currentStatus: 'idle',
    now: 1_700_000_000_000,
    sequence: 1,
    ...overrides,
  };
}

function applyPatches(patches: ReturnType<typeof reduceChunk>['messageUpserts']): void {
  for (const u of patches) {
    if (u.op === 'append' && u.message) {
      const cur = messageStore.get().messages['sid-1'] ?? [];
      messageStore.set({ messages: { ...messageStore.get().messages, 'sid-1': [...cur, u.message] } });
    } else if (u.op === 'patch' && u.messageId && u.patch) {
      const cur = messageStore.get().messages['sid-1'] ?? [];
      const next = cur.map((m) => (m.id === u.messageId ? { ...m, ...u.patch } : m));
      messageStore.set({ messages: { ...messageStore.get().messages, 'sid-1': next } });
    } else if (u.op === 'remove' && u.messageId) {
      const cur = messageStore.get().messages['sid-1'] ?? [];
      messageStore.set({
        messages: { ...messageStore.get().messages, 'sid-1': cur.filter((m) => m.id !== u.messageId) },
      });
    }
  }
}

beforeEach(() => __resetAllStoresForTest());

describe('applyChunk dispatch via reducer', () => {
  it('normalizeChunk returns null for non-chunks', () => {
    expect(normalizeChunk(null)).toBeNull();
    expect(normalizeChunk('foo')).toBeNull();
    expect(normalizeChunk({ kind: 'mystery' })).toBeNull();
  });

  it('delta → reducer → apply: appends first assistant', () => {
    const chunk = normalizeChunk({ kind: 'delta', body: { text: 'hi' }, sequence: 1 }) as AnyChatChunk;
    const result = reduceChunk(chunk, snap());
    applyPatches(result.messageUpserts);
    const msgs = messageStore.get().messages['sid-1'];
    expect(msgs.length).toBe(1);
    expect(msgs[0]?.role).toBe('assistant');
    expect(msgs[0]?.content).toBe('hi');
    expect(result.statusHint).toBe('running');
  });

  it('tool → reducer → apply: patches toolCalls onto assistant', () => {
    const seq = [1, 2];
    const first = reduceChunk(
      normalizeChunk({ kind: 'delta', body: { text: '' }, sequence: seq[0] }) as AnyChatChunk,
      snap(),
    );
    applyPatches(first.messageUpserts);
    const tool = reduceChunk(
      normalizeChunk({ kind: 'tool', body: { tool_call_id: 'tc-1', phase: 'start', name: 'search' }, sequence: seq[1] }) as AnyChatChunk,
      snap({
        messages: messageStore.get().messages['sid-1'] ?? [],
        book: first.replaceBook!,
        sequence: seq[1],
      }),
    );
    applyPatches(tool.messageUpserts);
    const msgs = messageStore.get().messages['sid-1'];
    expect(msgs[0]?.toolCalls?.length).toBe(1);
    expect(msgs[0]?.toolCalls?.[0]?.toolCallId).toBe('tc-1');
  });

  it('final → reducer → apply: marks streaming false + finalize', () => {
    const delta = reduceChunk(
      normalizeChunk({ kind: 'delta', body: { text: 'ans' }, sequence: 1 }) as AnyChatChunk,
      snap(),
    );
    applyPatches(delta.messageUpserts);
    const final = reduceChunk(
      normalizeChunk({ kind: 'final', body: { text: 'ans' }, sequence: 2 }) as AnyChatChunk,
      snap({
        messages: messageStore.get().messages['sid-1'] ?? [],
        book: delta.replaceBook!,
        sequence: 2,
        now: 1_700_000_005_000,
      }),
    );
    applyPatches(final.messageUpserts);
    expect(final.finalize).toBe(true);
    expect(final.turn?.status).toBe(200);
    const msgs = messageStore.get().messages['sid-1'];
    expect(msgs[0]?.streaming).toBe(false);
  });

  it('plan_review → reducer: mutates book.pendingPlan and attaches planReview to turn message', () => {
    const r = reduceChunk(
      normalizeChunk({ kind: 'plan_review', body: { plan: 'do A', plan_file: 'f.md' }, sequence: 1 }) as AnyChatChunk,
      snap(),
    );
    expect(r.replaceBook?.pendingPlan).toEqual({ plan: 'do A', planFile: 'f.md', status: 'pending' });
    // planReview 挂到回合消息（无 assistantId 时 append 一条空 assistant 消息）
    expect(r.messageUpserts).toHaveLength(1);
    expect(r.messageUpserts[0]?.op).toBe('append');
    expect(r.messageUpserts[0]?.message?.planReview).toMatchObject({
      plan: 'do A',
      planFile: 'f.md',
      status: 'pending',
    });
    expect(r.statusHint).toBe('idle');
  });

  it('unknown kind → reducer: no-op', () => {
    const r = reduceChunk({ kind: '__unknown_kind__', body: {}, sequence: 9 } as AnyChatChunk, snap());
    expect(r.messageUpserts).toEqual([]);
    expect(r.statusHint).toBeUndefined();
  });
});

describe('resolveBusyTransition', () => {
  it('running/queued statusHint → busy true', () => {
    expect(resolveBusyTransition('delta', 'running')).toBe(true);
    expect(resolveBusyTransition('status', 'queued')).toBe(true);
  });

  it('idle/error statusHint → busy false', () => {
    expect(resolveBusyTransition('final', 'idle')).toBe(false);
    expect(resolveBusyTransition('error', 'error')).toBe(false);
  });

  it('user-wait kinds → busy false even without statusHint', () => {
    expect(resolveBusyTransition('followup_question', undefined)).toBe(false);
    expect(resolveBusyTransition('plan_review', undefined)).toBe(false);
  });

  it('presentation-only staffing lifecycle does not change busy state', () => {
    expect(resolveBusyTransition('followup_question', undefined, false, true)).toBeNull();
  });

  it('post-turn auxiliary chunks → do not flip busy back on', () => {
    expect(resolveBusyTransition('todo_updated', undefined)).toBeNull();
    expect(resolveBusyTransition('file_changes', undefined)).toBeNull();
  });

  it('turnSealed blocks late running hints', () => {
    expect(resolveBusyTransition('delta', 'running', true)).toBeNull();
    expect(resolveBusyTransition('delta', 'running', false)).toBe(true);
  });
});

describe('resolveTurnGate', () => {
  it('accepts matching in-flight request frames', () => {
    expect(resolveTurnGate('delta', 'req-1', {
      turnSealed: false,
      activeRequestId: 'req-1',
      acceptingNewRequest: false,
    })).toEqual({ action: 'accept' });
  });

  it('drops stale frames from a different request even while a new turn is open', () => {
    expect(resolveTurnGate('delta', 'old-req', {
      turnSealed: false,
      activeRequestId: 'new-req',
      acceptingNewRequest: false,
    })).toEqual({ action: 'drop' });
  });

  it('drops late generation frames after the matching request is sealed', () => {
    expect(resolveTurnGate('tool', 'req-1', {
      turnSealed: true,
      activeRequestId: 'req-1',
      acceptingNewRequest: false,
    })).toEqual({ action: 'drop' });
  });

  it('binds the first request frame after backend-live recovery', () => {
    expect(resolveTurnGate('delta', 'req-recovered', {
      turnSealed: false,
      activeRequestId: null,
      acceptingNewRequest: true,
    })).toEqual({ action: 'accept', bindRequestId: 'req-recovered' });
  });

  it('binds request-scoped wait frames after backend-live recovery', () => {
    const gate = { turnSealed: false, activeRequestId: null, acceptingNewRequest: true };
    expect(resolveTurnGate('plan_review', 'req-plan', gate)).toEqual({ action: 'accept', bindRequestId: 'req-plan' });
    expect(resolveTurnGate('followup_question', 'req-follow', gate)).toEqual({ action: 'accept', bindRequestId: 'req-follow' });
  });

  it('allows non-generation frames after final so plan review and inspector updates survive', () => {
    const gate = { turnSealed: true, activeRequestId: 'req-1', acceptingNewRequest: false };
    expect(resolveTurnGate('plan_review', 'req-1', gate)).toEqual({ action: 'accept' });
    expect(resolveTurnGate('todo_updated', 'req-1', gate)).toEqual({ action: 'accept' });
    expect(resolveTurnGate('file_changes', 'req-1', gate)).toEqual({ action: 'accept' });
  });

  it('drops stale request-scoped auxiliary frames from a previous request', () => {
    const gate = { turnSealed: false, activeRequestId: 'req-new', acceptingNewRequest: false };
    expect(resolveTurnGate('plan_review', 'req-old', gate)).toEqual({ action: 'drop' });
    expect(resolveTurnGate('todo_updated', 'req-old', gate)).toEqual({ action: 'drop' });
    expect(resolveTurnGate('file_changes', 'req-old', gate)).toEqual({ action: 'drop' });
  });

  it('accepts task frames for matching in-flight request even when turn is sealed', () => {
    expect(resolveTurnGate('task', 'req-1', {
      turnSealed: true,
      activeRequestId: 'req-1',
      acceptingNewRequest: false,
    })).toEqual({ action: 'accept' });
  });
});
