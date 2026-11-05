import { describe, it, expect } from 'vitest';
import { mergeBackendHistory, prependHistoryWindow } from '../../src/ui/features/history-merge';
import type { ChatMessage } from '../../src/ui/chat-render';

function msg(partial: Partial<ChatMessage> & Pick<ChatMessage, 'role' | 'content'>): ChatMessage {
  return {
    id: partial.id ?? 'm1',
    role: partial.role,
    content: partial.content,
    timestamp: partial.timestamp ?? 1,
    ...partial,
  };
}

describe('mergeBackendHistory', () => {
  it('replaces entirely when idle', () => {
    const local = [msg({ role: 'user', content: 'a' })];
    const remote = [msg({ role: 'user', content: 'a' }), msg({ role: 'assistant', content: 'done', id: 'a2' })];
    expect(mergeBackendHistory(local, remote, { live: 'idle' })).toEqual(remote);
  });

  it('preserves streaming tail when running', () => {
    const local = [
      msg({ role: 'user', content: 'q', id: 'u1' }),
      msg({ role: 'assistant', content: 'partial...', id: 'a1', streaming: true }),
    ];
    const remote = [msg({ role: 'user', content: 'q', id: 'u2' })];
    const merged = mergeBackendHistory(local, remote, { live: 'running', preserveLocalTail: true });
    expect(merged).toHaveLength(2);
    expect(merged[1].content).toBe('partial...');
    expect(merged[1].streaming).toBe(true);
  });

  it('preserves the local in-flight user turn before the assistant starts streaming', () => {
    const local = [
      msg({ role: 'user', content: 'previous', id: 'u1' }),
      msg({ role: 'assistant', content: 'done', id: 'a1' }),
      msg({ role: 'user', content: 'current question', id: 'u2' }),
      msg({ role: 'status', content: '正在处理', id: 's1' }),
    ];
    const remote = [
      msg({ role: 'user', content: 'previous', id: 'ru1' }),
      msg({ role: 'assistant', content: 'done', id: 'ra1' }),
    ];
    const merged = mergeBackendHistory(local, remote, { live: 'running', preserveLocalTail: true });
    expect(merged.map((m) => m.content)).toEqual(['previous', 'done', 'current question', '正在处理']);
  });

  it('does not duplicate remote messages that overlap the preserved tail', () => {
    const local = [
      msg({ role: 'user', content: 'q1', id: 'u1' }),
      msg({ role: 'assistant', content: 'a1', id: 'a1' }),
      msg({ role: 'user', content: 'q2', id: 'u2' }),
      msg({ role: 'assistant', content: 'partial', id: 'a2', streaming: true }),
    ];
    const remote = [
      msg({ role: 'user', content: 'q1', id: 'ru1' }),
      msg({ role: 'assistant', content: 'a1', id: 'ra1' }),
      msg({ role: 'user', content: 'q2', id: 'ru2' }),
    ];
    const merged = mergeBackendHistory(local, remote, { live: 'running', preserveLocalTail: true });
    expect(merged.map((m) => m.content)).toEqual(['q1', 'a1', 'q2', 'partial']);
  });

  it('keeps longer local completed history during replay accumulation', () => {
    const local = [
      msg({ role: 'user', content: 'q', id: 'u1' }),
      msg({ role: 'assistant', content: 'old answer', id: 'a1' }),
      msg({ role: 'assistant', content: 'streaming', id: 'a2', streaming: true }),
    ];
    const remote = [msg({ role: 'user', content: 'q', id: 'u2' })];
    const merged = mergeBackendHistory(local, remote, { live: 'running', preserveLocalTail: true });
    expect(merged.map((m) => m.content)).toEqual(['q', 'old answer', 'streaming']);
  });
});

describe('prependHistoryWindow（P1-6 触顶翻页前缀拼接）', () => {
  it('正常翻页（互斥游标无重叠）直接前缀拼接', () => {
    const current = [msg({ role: 'user', content: 'q2', id: 'c1' }), msg({ role: 'assistant', content: 'a2', id: 'c2' })];
    const older = [msg({ role: 'user', content: 'q1', id: 'o1' }), msg({ role: 'assistant', content: 'a1', id: 'o2' })];
    const merged = prependHistoryWindow(current, older);
    expect(merged.map((m) => m.content)).toEqual(['q1', 'a1', 'q2', 'a2']);
  });

  it('older 尾部与 current 头部重复（历史被 rewind/fork 重写）时裁掉重叠段', () => {
    const current = [msg({ role: 'user', content: 'q2' }), msg({ role: 'assistant', content: 'a2' })];
    const older = [
      msg({ role: 'user', content: 'q0' }),
      msg({ role: 'user', content: 'q2' }),
      msg({ role: 'assistant', content: 'a2' }),
    ];
    const merged = prependHistoryWindow(current, older);
    expect(merged.map((m) => m.content)).toEqual(['q0', 'q2', 'a2']);
  });

  it('空侧处理：older 空返回 current，current 空返回 older', () => {
    const current = [msg({ role: 'user', content: 'q' })];
    expect(prependHistoryWindow(current, [])).toBe(current);
    expect(prependHistoryWindow([], current)).toBe(current);
  });
});
