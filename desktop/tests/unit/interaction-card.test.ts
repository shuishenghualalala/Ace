/** @vitest-environment happy-dom */
import { describe, expect, it, vi } from 'vitest';

import {
  markInteractionSubmitted,
  renderToolInteractionCard,
  syncInteractionCards,
} from '../../src/ui/components/interaction-card';
import type { ToolCallInfo } from '../../src/ui/chat-render';

function tool(overrides: Partial<ToolCallInfo>): ToolCallInfo {
  return {
    toolCallId: 'tool-1',
    name: 'wiki_learning_activity',
    status: 'done',
    startedAt: 1,
    ...overrides,
  };
}

describe('interaction card', () => {
  it('renders a source-grounded single choice card and submits a normal reply', () => {
    const card = renderToolInteractionCard(tool({
      args: JSON.stringify({ action: 'create' }),
      result: JSON.stringify({
        activity: {
          id: 'activity-1',
          activity_type: 'quiz',
          prompt: '**2030 年**常住人口城镇化率目标约为多少？',
          evidence_page_ids: ['page-1', 'page-2'],
          public_payload: {
            schema: 'crew.interaction.v1',
            title: '城镇化目标',
            interaction: {
              kind: 'single_choice',
              options: [
                { id: 'A', label: '68%' },
                { id: 'B', label: '71%', description: '规划目标值' },
              ],
            },
            progress: { current: 1, total: 5 },
          },
        },
      }),
    }));

    expect(card).not.toBeNull();
    expect(card?.textContent).toContain('城镇化目标');
    expect(card?.textContent).toContain('1 / 5');
    expect(card?.textContent).toContain('依据 2 个 Wiki 页面');
    expect(card?.querySelectorAll('[data-interaction-option]')).toHaveLength(2);
    expect(Array.from(card?.querySelectorAll('.followup-card__option-key') ?? [])
      .map((item) => item.textContent)).toEqual(['A', 'B']);
    expect(card?.classList.contains('followup-card')).toBe(true);
    expect(card?.querySelector('.chat-markdown strong')?.textContent).toBe('2030 年');

    const submitted = vi.fn();
    card?.addEventListener('crew:interaction-submit', submitted);
    const selected = card?.querySelector<HTMLInputElement>('[data-interaction-option="B"]');
    if (selected) {
      selected.checked = true;
      selected.dispatchEvent(new Event('change', { bubbles: true }));
    }
    expect(submitted).toHaveBeenCalledOnce();
    expect((submitted.mock.calls[0][0] as CustomEvent).detail).toEqual({
      interactionId: 'activity-1',
      text: '我选择 B：71%',
    });
  });

  it('renders assessment feedback without exposing the raw answer', () => {
    const card = renderToolInteractionCard(tool({
      name: 'wiki_learning_assess',
      result: JSON.stringify({
        assessment: {
          activity_id: 'activity-1',
          summary: '方向正确，能区分两种并发模型。',
          score: 0.85,
          strengths: ['场景判断正确'],
          gaps: ['补充调度模型'],
          evidence_page_ids: ['page-1'],
        },
      }),
    }));

    expect(card?.dataset.interactionCompletion).toBe('activity-1');
    expect(card?.textContent).toContain('85%');
    expect(card?.textContent).toContain('做得好的地方');
    expect(card?.textContent).toContain('可以继续加强');
    expect(card?.textContent).not.toContain('response_text');
  });

  it('renders an editable text answer and submits the trimmed response', () => {
    const card = renderToolInteractionCard(tool({
      args: JSON.stringify({ action: 'create' }),
      result: JSON.stringify({
        activity: {
          id: 'activity-text',
          activity_type: 'interview',
          prompt: '请说明线程和 asyncio 的适用场景。',
          public_payload: {
            schema: 'crew.interaction.v1',
            interaction: { kind: 'text' },
          },
        },
      }),
    }));

    const answer = card?.querySelector<HTMLTextAreaElement>('[data-interaction-answer]');
    const submit = card?.querySelector<HTMLButtonElement>('[data-interaction-submit]');
    expect(answer?.placeholder).toBe('在下方输入你的回答…');
    expect(submit?.disabled).toBe(true);

    const submitted = vi.fn();
    card?.addEventListener('crew:interaction-submit', submitted);
    if (answer && submit) {
      answer.value = '  线程适合阻塞 I/O，asyncio 适合协作式 I/O。  ';
      answer.dispatchEvent(new Event('input', { bubbles: true }));
      expect(submit.disabled).toBe(false);
      submit.click();
    }

    expect(submitted).toHaveBeenCalledOnce();
    expect((submitted.mock.calls[0][0] as CustomEvent).detail).toEqual({
      interactionId: 'activity-text',
      text: '线程适合阻塞 I/O，asyncio 适合协作式 I/O。',
    });
  });

  it('keeps only the latest unresolved card interactive', () => {
    const container = document.createElement('div');
    const first = document.createElement('section');
    first.className = 'interaction-card';
    first.dataset.interactionId = 'activity-old';
    first.appendChild(document.createElement('input')).dataset.interactionOption = 'A';
    const latest = document.createElement('section');
    latest.className = 'interaction-card';
    latest.dataset.interactionId = 'activity-new';
    const latestInput = document.createElement('input');
    latestInput.dataset.interactionOption = 'B';
    const latestAnswer = document.createElement('textarea');
    latestAnswer.dataset.interactionAnswer = '1';
    latest.append(latestInput, latestAnswer);
    container.append(first, latest);

    syncInteractionCards(container);
    expect(first.querySelector('input')?.disabled).toBe(true);
    expect(latest.querySelector('input')?.disabled).toBe(false);
    expect(latestAnswer.disabled).toBe(false);

    markInteractionSubmitted('activity-new');
    syncInteractionCards(container);
    expect(latest.querySelector('input')?.disabled).toBe(true);
    expect(latestAnswer.disabled).toBe(true);
  });

  it('uses text nodes for untrusted card content', () => {
    const card = renderToolInteractionCard(tool({
      args: JSON.stringify({ action: 'create' }),
      result: JSON.stringify({
        activity: {
          id: 'activity-xss',
          activity_type: 'quiz',
          prompt: '<img src=x onerror=alert(1)>',
          public_payload: {
            schema: 'crew.interaction.v1',
            interaction: { options: [{ id: 'A', label: '<script>alert(1)</script>' }] },
          },
        },
      }),
    }));

    expect(card?.querySelector('img')).toBeNull();
    expect(card?.querySelector('script')).toBeNull();
    expect(card?.querySelector('.chat-markdown')).not.toBeNull();
  });
});
