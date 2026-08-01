import { describe, expect, it } from 'vitest';
import { isAssistantNarrationContent, hasVisibleAnswerText, type ChatMessage } from '../../src/ui/chat-render';

describe('isAssistantNarrationContent', () => {
  const msg = (overrides: Partial<ChatMessage> = {}): ChatMessage => ({
    id: 'm1',
    role: 'assistant',
    content: 'text',
    timestamp: 1,
    ...overrides,
  });

  it('treats earlier assistant text as narration', () => {
    expect(isAssistantNarrationContent(0, 1, msg())).toBe(true);
  });

  it('treats tool-accompanied last text as narration', () => {
    expect(
      isAssistantNarrationContent(
        0,
        0,
        msg({ toolCalls: [{ toolCallId: 't1', name: 'terminal', status: 'running', startedAt: 1 }] }),
      ),
    ).toBe(true);
  });

  it('treats final text without tools as visible answer', () => {
    expect(isAssistantNarrationContent(0, 0, msg({ content: '最终回答' }))).toBe(false);
  });

  it('respects segmentRole answer over legacy toolCalls heuristic', () => {
    expect(
      isAssistantNarrationContent(
        0,
        0,
        msg({
          content: '最终回答',
          segmentRole: 'answer',
          toolCalls: [{ toolCallId: 't1', name: 'x', status: 'done', startedAt: 1 }],
        }),
      ),
    ).toBe(false);
  });

  it('respects segmentRole process', () => {
    expect(isAssistantNarrationContent(0, 0, msg({ content: '旁白', segmentRole: 'process' }))).toBe(true);
  });
});

describe('hasVisibleAnswerText', () => {
  const msg = (overrides: Partial<ChatMessage> = {}): ChatMessage => ({
    id: 'm1',
    role: 'assistant',
    content: '',
    timestamp: 1,
    ...overrides,
  });

  it('returns false when only process narration exists', () => {
    expect(
      hasVisibleAnswerText([
        msg({ id: 'm1', content: '我先查一下。', segmentRole: 'process', toolCalls: [{ toolCallId: 't1', name: 'x', status: 'running', startedAt: 1 }] }),
      ]),
    ).toBe(false);
  });

  it('returns false for uncertain pre-tool text marked process (no auto-fold yet)', () => {
    // 工具尚未到达：首段字标 process，不算正式正文，避免过程区先折再展
    expect(
      hasVisibleAnswerText([
        msg({ id: 'm1', content: '我先了解项目空间。', segmentRole: 'process', streaming: true }),
      ]),
    ).toBe(false);
  });

  it('returns true when answer segment has text', () => {
    expect(
      hasVisibleAnswerText([
        msg({ id: 'm1', content: '', segmentRole: 'process' }),
        msg({ id: 'm2', content: '最终', segmentRole: 'answer' }),
      ]),
    ).toBe(true);
  });
});
