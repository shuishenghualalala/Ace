/**
 * applyChunk dispatch 适配层单测。
 * 目标：applyChunk 应退化为「normalize → reduce → apply patch → side effects」
 * 薄适配层，所有 7 个 kind 的状态迁移由 chat-reducer 提供（见 chat-reducer.test.ts）。
 * 本文件只保留 busy 迁移与回合门控两个纯函数 dispatch 的覆盖。
 */
import { describe, it, expect } from 'vitest';
import {
  resolveBusyTransition,
  resolveTurnGate,
} from '../../src/ui/reducers/chat-reducer';

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
