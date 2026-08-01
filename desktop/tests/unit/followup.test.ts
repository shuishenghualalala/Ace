/** @vitest-environment happy-dom */

import { describe, expect, it, vi } from 'vitest';
import {
  bindFollowupCard,
  formatFollowupAnswerMessage,
  renderFollowupCard,
  renderFollowupCardElement,
} from '../../src/ui/followup';
import type { PendingFollowup } from '../../src/ui/state';

function permissionQuestion(): PendingFollowup {
  return {
    questionId: 'permission-1',
    title: '权限确认 · browser_use',
    recordHistory: false,
    questions: [{
      id: 'perm',
      question: '即将执行：{"action":"press","key":"Enter"}\n\n原因：可能提交表单',
      options: [
        { label: '允许一次', value: 'allow_once' },
        { label: '拒绝', value: 'deny' },
      ],
      allowFreeText: false,
      multiSelect: false,
    }],
  };
}

describe('follow-up permission presentation', () => {
  it('uses the shared Team Logo and team name for control-plane permissions', () => {
    const question = permissionQuestion();
    question.origin = { type: 'team_control', agentName: '像素开发小游戏团队' };

    const html = renderFollowupCard(question);

    expect(html).toContain('followup-card__source--team');
    expect(html).toContain('session__team-logo');
    expect(html).toContain('像素开发小游戏团队');
    expect(html).not.toContain('像素开发小游戏团队 正在询问');
    expect(html).not.toContain('其他（自定义输入）');
  });

  it('does not render an arbitrary custom-answer option for permissions', () => {
    const html = renderFollowupCard(permissionQuestion());
    expect(html).toContain('允许一次');
    expect(html).toContain('followup-card--permission');
    expect(html).toContain('允许提交网页表单？');
    expect(html).toContain('按下 Enter');
    expect(html).toContain('此操作会向当前网站提交输入内容');
    expect(html).not.toContain('权限确认 · browser_use');
    expect(html).not.toContain('{&quot;action&quot;');
    expect(html).not.toContain('为什么需要确认');
    expect(html).not.toContain('role="radiogroup"');
    expect(html).toContain('role="alertdialog"');
    expect(html).toContain('aria-modal="true"');
    expect(html).not.toContain('其他（自定义输入）');
    expect(html).not.toContain('__free_text__');
  });

  it('submits a direct permission decision without a second confirmation step', () => {
    const root = document.createElement('div');
    root.innerHTML = renderFollowupCard(permissionQuestion());
    document.body.appendChild(root);
    const onSubmit = vi.fn();
    bindFollowupCard(root, { onSubmit, onCancel: () => undefined });

    const deny = root.querySelector<HTMLButtonElement>('[data-permission-value="deny"]')!;
    expect(document.activeElement).toBe(deny);
    deny.click();
    deny.click();

    expect(onSubmit).toHaveBeenCalledWith('permission-1', [
      { question_id: 'perm', answers: ['deny'] },
    ]);
    expect(onSubmit).toHaveBeenCalledOnce();
    root.remove();
  });

  it('translates browser type arguments into readable copy and centers through the modal wrapper', () => {
    const question = permissionQuestion();
    question.questions[0]!.question = '即将执行：{"action":"type","ref":"p1:e27","submit":true,"text":"云南旅游视频"}\n\n原因：填写后按 Enter 可能提交表单，需要一次性确认';

    const element = renderFollowupCardElement(question);
    expect(element.classList.contains('followup-card-wrap--permission')).toBe(true);
    expect(element.textContent).toContain('输入“云南旅游视频”并提交');
    expect(element.textContent).not.toContain('p1:e27');
    expect(element.textContent).not.toContain('action');
  });

  it('supports split browser tool names without exposing their arguments', () => {
    const question = permissionQuestion();
    question.title = '权限确认 · browser_type';
    question.questions[0]!.question = '即将执行：{"ref":"p8:e4","submit":true,"text":"搜索内容"}\n\n原因：填写后按 Enter 可能提交表单，需要一次性确认';

    const html = renderFollowupCard(question);
    expect(html).toContain('允许提交网页表单？');
    expect(html).toContain('输入“搜索内容”并提交');
    expect(html).not.toContain('p8:e4');
  });

  it('treats Escape as a safe cancellation', () => {
    const root = document.createElement('div');
    root.innerHTML = renderFollowupCard(permissionQuestion());
    const onCancel = vi.fn();
    bindFollowupCard(root, { onSubmit: () => undefined, onCancel });

    root.querySelector<HTMLElement>('.followup-card')?.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }),
    );
    expect(onCancel).toHaveBeenCalledWith('permission-1');
  });

  it('does not turn an internal approval value into a user message', () => {
    expect(formatFollowupAnswerMessage(permissionQuestion(), [
      { question_id: 'perm', answers: ['allow_once'] },
    ])).toBeNull();
  });

  it('uses option labels instead of internal values for recorded follow-ups', () => {
    const question = permissionQuestion();
    question.recordHistory = true;
    expect(formatFollowupAnswerMessage(question, [
      { question_id: 'perm', answers: ['allow_once'] },
    ])).toBe('已选择：允许一次');
  });
});
