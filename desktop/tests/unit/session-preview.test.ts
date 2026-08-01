/**
 * @vitest-environment happy-dom
 */
import { describe, expect, it } from 'vitest';
import { renderConversationPreview } from '../../src/ui/chat-render';
import type { ChatMessage } from '../../src/ui/chat-render';

describe('renderConversationPreview', () => {
  it('renders user and agent messages without edit buttons', () => {
    const container = document.createElement('div');
    const messages: ChatMessage[] = [
      { id: 'u1', role: 'user', content: '你好', timestamp: Date.now() },
      { id: 'a1', role: 'assistant', content: '你好，有什么可以帮你？', timestamp: Date.now() },
    ];

    renderConversationPreview(container, messages, 'test-model');

    expect(container.querySelectorAll('.msg').length).toBe(2);
    expect(container.querySelector('.msg.user .msg__text')?.textContent).toBe('你好');
    expect(container.querySelector('[data-edit]')).toBeNull();
  });

  it('shows empty state when no messages', () => {
    const container = document.createElement('div');
    renderConversationPreview(container, [], 'test-model');
    expect(container.querySelector('.session-preview-empty')?.textContent).toContain('暂无消息');
  });
});
