/**
 * @vitest-environment node
 */
import { describe, expect, it } from 'vitest';
import { formatTurnDuration, resolveTurnDurationMs, type ChatMessage } from '../../src/ui/chat-render';

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

describe('formatTurnDuration（中文计时头时长）', () => {
  it('不足 1 秒按 1 秒显示，避免「0 秒」', () => {
    expect(formatTurnDuration(0)).toBe('1 秒');
    expect(formatTurnDuration(500)).toBe('1 秒');
  });

  it('1 秒至 59 秒显示「N 秒」', () => {
    expect(formatTurnDuration(1_000)).toBe('1 秒');
    expect(formatTurnDuration(42_000)).toBe('42 秒');
    expect(formatTurnDuration(59_999)).toBe('59 秒');
  });

  it('满分钟显示「N 分」，带秒显示「N 分 N 秒」', () => {
    expect(formatTurnDuration(60_000)).toBe('1 分');
    expect(formatTurnDuration(622_000)).toBe('10 分 22 秒');
    expect(formatTurnDuration(3_599_000)).toBe('59 分 59 秒');
  });

  it('满小时显示「N 小时」，带分钟显示「N 小时 N 分」', () => {
    expect(formatTurnDuration(3_600_000)).toBe('1 小时');
    expect(formatTurnDuration(3_660_000)).toBe('1 小时 1 分');
  });

  it('负值按 0 处理', () => {
    expect(formatTurnDuration(-5_000)).toBe('1 秒');
  });
});
