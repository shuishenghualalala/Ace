/**
 * @vitest-environment happy-dom
 *
 * Inspector「计划」页签应同时展示：
 *   1. Plan 模式批准后的计划正文（book.pendingPlan）
 *   2. todo 工具进度列表（book.todos）
 * 不能只剩 checklist，也不能在有计划无 todo 时显示空态。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { __resetAllStoresForTest, configStore, messageStore, sessionStore } from '../../src/ui/stores/stores';
import { ensureSessionBook, patchBook, setActiveSessionId, setBookTodos } from '../../src/ui/state';
import { openInspectorToTab, refreshInspector, setPlanBoardActions } from '../../src/ui/features/inspector';

vi.mock('../../src/ui/backend-client', () => ({
  backendApi: {
    sessionContext: vi.fn(async () => ({ used_tokens: 0, max_tokens: 0, ratio: 0 })),
  },
}));

beforeEach(() => {
  __resetAllStoresForTest();
  configStore.set({
    config: {
      model: 'm',
      has_key: true,
      base_url: 'https://api.example.com/v1',
      active_model_id: 'm1',
      models: [{ id: 'm1', name: 'M1', model: 'm', base_url: 'https://api.example.com/v1', context_window: 128000, has_key: true, loaded: true }],
    },
  });
  setActiveSessionId('sess-plan');
  sessionStore.set({
    sessions: [{ id: 'sess-plan', title: 'Plan Sess', workspaceId: 'default', updatedAt: 1, preview: '', badge: '' }],
  });
  messageStore.set({
    messages: { 'sess-plan': [{ id: 'u1', role: 'user', content: '做俄罗斯方块', timestamp: 1 }] },
  });
  ensureSessionBook('sess-plan');
  document.body.innerHTML = `
    <div id="chat-inspector"><div id="chat-inspector-body"></div></div>
    <button id="task-board-toggle"></button>
  `;
});

describe('Inspector「计划」页签：计划正文 + todo 同步', () => {
  it('同时展示 pendingPlan 正文与 todo 进度', () => {
    patchBook('sess-plan', {
      pendingPlan: {
        plan: '# 俄罗斯方块游戏开发计划\n\n目标：做一个好看的俄罗斯方块。',
        planFile: 'C:\\Users\\u\\.Crew\\plans\\plan.md',
        status: 'readonly',
      },
    });
    setBookTodos('sess-plan', [
      { id: 't1', content: '创建游戏基础框架', status: 'completed' },
      { id: 't2', content: '实现方块控制', status: 'in_progress' },
      { id: 't3', content: '美化 UI', status: 'pending' },
    ]);

    openInspectorToTab('plan');
    const body = document.getElementById('chat-inspector-body');
    const text = body?.textContent ?? '';
    const html = body?.innerHTML ?? '';

    expect(html).toContain('inspector-plan-board');
    expect(text).toContain('计划方案');
    expect(text).toContain('俄罗斯方块游戏开发计划');
    expect(text).toContain('目标：做一个好看的俄罗斯方块');
    expect(text).toContain('任务进度');
    expect(text).toContain('创建游戏基础框架');
    expect(text).toContain('实现方块控制');
    expect(text).toContain('美化 UI');
    expect(text).toMatch(/1\s*\/\s*3/);
    // 历史只读：无审批动作
    expect(text).toContain('历史计划只读展示');
    expect(html).not.toContain('data-plan-board-action="approve"');
  });

  it('待审批计划展示编辑/预览切换与四动作', () => {
    patchBook('sess-plan', {
      pendingPlan: {
        plan: '# 仅计划\n\n先写方案再拆任务。',
        planFile: 'plans/only.md',
        status: 'pending',
      },
    });

    openInspectorToTab('plan');
    const body = document.getElementById('chat-inspector-body');
    const text = body?.textContent ?? '';
    const html = body?.innerHTML ?? '';
    expect(text).toContain('计划方案');
    expect(text).toContain('仅计划');
    expect(text).toContain('等待审批');
    expect(text).toContain('批准并执行');
    expect(text).toContain('撤销');
    expect(text).toContain('其他');
    expect(html).toContain('data-plan-mode="edit"');
    expect(html).toContain('data-plan-board-action="approve"');
    expect(text).not.toContain('当前会话没有可抽取的计划项');
  });

  it('仅有 todo、无 pendingPlan 时仍展示任务进度', () => {
    setBookTodos('sess-plan', [
      { id: 't1', content: '只做 checklist', status: 'pending' },
    ]);

    openInspectorToTab('plan');
    const text = document.getElementById('chat-inspector-body')?.textContent ?? '';
    expect(text).toContain('任务进度');
    expect(text).toContain('只做 checklist');
    expect(text).not.toContain('计划方案');
  });

  it('计划与 todo 皆空时显示空态引导', () => {
    openInspectorToTab('plan');
    const text = document.getElementById('chat-inspector-body')?.textContent ?? '';
    expect(text).toContain('执行计划');
    expect(text).toContain('无');
    expect(text).toMatch(/计划|todo|任务/i);
  });

  it('refreshInspector 重渲后保留计划正文滚动位置', () => {
    const longPlan = [
      '# 长计划',
      '',
      ...Array.from({ length: 80 }, (_, i) => `- 步骤 ${i + 1}：详细说明以便产生可滚动高度`),
    ].join('\n');
    patchBook('sess-plan', {
      pendingPlan: {
        plan: longPlan,
        planFile: 'plans/long.md',
        status: 'pending',
      },
    });
    setBookTodos('sess-plan', [
      { id: 't1', content: '进行中', status: 'in_progress' },
    ]);

    openInspectorToTab('plan');
    const body = document.getElementById('chat-inspector-body') as HTMLElement;
    const doc = body.querySelector('.inspector-plan-board__doc-body') as HTMLElement | null;
    expect(doc).toBeTruthy();

    // happy-dom 无真实布局：用 WeakMap 劫持 scrollTop，使重渲后的新节点也能读写。
    const tops = new WeakMap<Element, number>();
    const desc = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'scrollTop');
    Object.defineProperty(HTMLElement.prototype, 'scrollTop', {
      configurable: true,
      get(this: HTMLElement) {
        return tops.get(this) ?? 0;
      },
      set(this: HTMLElement, v: number) {
        tops.set(this, Number(v) || 0);
      },
    });
    try {
      doc!.scrollTop = 420;
      expect(doc!.scrollTop).toBe(420);

      setBookTodos('sess-plan', [
        { id: 't1', content: '进行中', status: 'in_progress' },
        { id: 't2', content: '下一步', status: 'pending' },
      ]);
      refreshInspector();

      const doc2 = body.querySelector('.inspector-plan-board__doc-body') as HTMLElement | null;
      expect(doc2).toBeTruthy();
      expect(doc2).not.toBe(doc);
      expect(doc2!.scrollTop).toBe(420);
    } finally {
      if (desc) Object.defineProperty(HTMLElement.prototype, 'scrollTop', desc);
    }
  });

  it('任务全部完成且非 planActive 时，残留 pending 强制显示为已批准只读', () => {
    patchBook('sess-plan', {
      planActive: false,
      pendingPlan: {
        plan: '# 水果忍者\n\n已落地。',
        planFile: 'plans/fruit.md',
        status: 'pending',
      },
    });
    setBookTodos('sess-plan', [
      { id: 't1', content: '参考现有游戏', status: 'completed' },
      { id: 't2', content: '实现 lalala.html', status: 'completed' },
      { id: 't3', content: '自检', status: 'completed' },
      { id: 't4', content: '告知路径', status: 'completed' },
    ]);

    openInspectorToTab('plan');
    const text = document.getElementById('chat-inspector-body')?.textContent ?? '';
    const html = document.getElementById('chat-inspector-body')?.innerHTML ?? '';
    expect(text).toContain('已批准');
    expect(text).not.toContain('等待审批');
    expect(text).toContain('历史计划只读展示');
    expect(html).not.toContain('data-plan-board-action="approve"');
    expect(text).toMatch(/4\s*\/\s*4/);
  });
});


describe('versioned Plan board drafts', () => {
  let sequence = 0;
  function prepare() {
    const sessionId = 'plan-draft-' + (++sequence);
    setActiveSessionId(sessionId);
    ensureSessionBook(sessionId);
    messageStore.set({ messages: { ...messageStore.get().messages,
      [sessionId]: [{ id: sessionId + '-user', role: 'user', content: 'Plan this task', timestamp: 1 }],
    } });
    patchBook(sessionId, { planActive: true, pendingPlan: {
      plan: '# Original', planFile: 'plan.md', status: 'pending', version: 'version-one',
    } });
    openInspectorToTab('plan');
    const click = (selector: string) => document.querySelector<HTMLButtonElement>(selector)!.click();
    click('[data-plan-mode="edit"]');
    return { sessionId, click, editor: () => document.querySelector<HTMLTextAreaElement>('[data-plan-editor]')! };
  }
  function type(input: HTMLTextAreaElement, value: string) {
    input.value = value;
    input.dispatchEvent(new Event('input', { bubbles: true }));
  }

  it('keeps edited text after an unavailable submission', () => {
    const { click, editor } = prepare();
    setPlanBoardActions({ submit: (_input, notify) => notify({ status: 'unavailable' }), recheck: vi.fn() });
    type(editor(), '# Local draft');
    click('[data-plan-board-action="approve"]');
    expect(editor().value).toBe('# Local draft');
    expect(document.querySelector('[data-plan-submission-status="unavailable"]')).not.toBeNull();
  });

  it('keeps the old draft when the authoritative version changes and blocks its approval', () => {
    const { sessionId, click, editor } = prepare();
    const submit = vi.fn();
    setPlanBoardActions({ submit, recheck: vi.fn() });
    type(editor(), '# Local draft');
    patchBook(sessionId, { pendingPlan: { plan: '# New plan', planFile: 'plan.md', status: 'pending', version: 'version-two' } });
    refreshInspector();
    expect(editor().value).toBe('# Local draft');
    expect(document.querySelector('[data-plan-submission-status="stale"]')).not.toBeNull();
    expect(document.querySelector<HTMLButtonElement>('[data-plan-board-action="approve"]')?.disabled).toBe(true);
    click('[data-plan-load-current]');
    expect(editor().value).toBe('# New plan');
    click('[data-plan-board-action="approve"]');
    expect(submit).toHaveBeenCalledWith(expect.objectContaining({ expectedVersion: 'version-two', plan: '# New plan' }), expect.any(Function));
  });

  it('clears only the confirmed draft without recapturing the old textarea', () => {
    const { sessionId, click, editor } = prepare();
    setPlanBoardActions({
      submit: (_input, notify) => {
        const snapshot = {
          session_id: sessionId, plan: '# Accepted', plan_file: 'plan.md', status: 'approved',
          active: false, awaiting_approval: false, has_plan: true, version: 'version-two',
        };
        patchBook(sessionId, { planActive: false, pendingPlan: {
          plan: snapshot.plan, planFile: snapshot.plan_file, status: 'approved', version: snapshot.version,
        } });
        notify({ status: 'applied', planState: snapshot });
      },
      recheck: vi.fn(),
    });
    type(editor(), '# Accepted');
    click('[data-plan-board-action="approve"]');
    expect(document.querySelector('[data-plan-editor]')).toBeNull();
    expect(document.querySelector('[data-plan-submission-status="stale"]')).toBeNull();
    expect(document.querySelector('.inspector-plan-board__doc-body')?.textContent).toContain('Accepted');
  });

  it('does not clear newer edits after a late applied response', () => {
    const { click, editor } = prepare();
    let answer: Parameters<Parameters<typeof setPlanBoardActions>[0]['submit']>[1] | undefined;
    setPlanBoardActions({ submit: (_input, notify) => { answer = notify; }, recheck: vi.fn() });
    type(editor(), '# Submitted draft');
    click('[data-plan-board-action="approve"]');
    type(editor(), '# Newer local draft');
    answer!({ status: 'applied' });
    expect(editor().value).toBe('# Newer local draft');
  });

  it('keeps drafts separated across session switches and late responses', () => {
    const first = prepare();
    let answer: Parameters<Parameters<typeof setPlanBoardActions>[0]['submit']>[1] | undefined;
    setPlanBoardActions({ submit: (_input, notify) => { answer = notify; }, recheck: vi.fn() });
    type(first.editor(), '# First draft');
    first.click('[data-plan-board-action="approve"]');
    const second = prepare();
    type(second.editor(), '# Second draft');
    answer!({ status: 'applied' });
    expect(second.editor().value).toBe('# Second draft');
    setActiveSessionId(first.sessionId);
    openInspectorToTab('plan');
    expect(document.querySelector<HTMLTextAreaElement>('[data-plan-editor]')).toBeNull();
  });
});
