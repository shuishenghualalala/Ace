import { describe, it, expect } from 'vitest';
import { mergeBackendHistory } from '../../src/ui/features/history-merge';
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
