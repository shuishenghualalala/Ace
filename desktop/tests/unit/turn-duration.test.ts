/**
 * @vitest-environment node
 */
import { describe, expect, it } from 'vitest';
import { resolveTurnDurationMs, type ChatMessage } from '../../src/ui/chat-render';

function assistant(partial: Partial<ChatMessage> & { id: string }): ChatMessage {
  return {
    role: 'assistant',
    content: '',
    timestamp: partial.timestamp ?? 1_000,
    ...partial,
  };
}

describe('resolveTurnDurationMs', () => {
  it('uses max stored turnDurationMs across split assistant segments when done', () => {
    const batch: ChatMessage[] = [
      assistant({ id: 'a1', turnStartedAt: 1_000, content: '让我查一下' }),
      { id: 's1', role: 'status', content: 'running', timestamp: 5_000 },
      assistant({ id: 'a2', turnStartedAt: 1_000, turnDurationMs: 27_000, content: '最终结果' }),
    ];
    expect(resolveTurnDurationMs(batch, { isLive: false })).toBe(27_000);
  });

  it('live mode counts from first assistant turnStartedAt', () => {
    const batch: ChatMessage[] = [
      assistant({ id: 'a1', turnStartedAt: 10_000, streaming: false, content: '旁白' }),
      { id: 's1', role: 'status', content: 'tool', timestamp: 12_000 },
      assistant({ id: 'a2', turnStartedAt: 10_000, streaming: true }),
    ];
    expect(resolveTurnDurationMs(batch, { isLive: true, now: 17_000 })).toBe(7_000);
  });

  it('falls back to timestamp delta when no stored duration', () => {
    const batch: ChatMessage[] = [
      assistant({ id: 'a1', turnStartedAt: 1_000, timestamp: 1_000 }),
      assistant({ id: 'a2', turnStartedAt: 1_000, timestamp: 4_500 }),
    ];
    expect(resolveTurnDurationMs(batch, { isLive: false })).toBe(3_500);
  });
});
