/**
 * @vitest-environment happy-dom
 *
 * 回合身份边界：applyChunk 必须在写入消息 / book / status 前拒收旧 request 的生成帧。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
// 共享 mock 必须先于 chat-controller 加载，见 helpers/mock-chat-controller-deps.ts
import './helpers/mock-chat-controller-deps';
import {
  _resetTurnDurationTickerForTests,
  applyChunk,
  renderChat,
} from '../../src/ui/features/chat-controller';
import { updateActiveToolDurations } from '../../src/ui/chat-render';
import { clearFoldMemoryCache } from '../../src/ui/features/fold-state';
import { __resetAllStoresForTest, messageStore, sessionStore } from '../../src/ui/stores/stores';
import { appendSessionMessage, ensureSessionBook, patchBook, setActiveSessionId, setSessionStatus, state, type Bookkeeping } from '../../src/ui/state';
import type { ChatChunk } from '../../src/ui/backend-client';

vi.mock('../../src/ui/features/workspaces', () => ({
  refreshAllSessions: vi.fn(async () => undefined),
  renderWorkspaceHistory: vi.fn(),
  commitDraftSession: vi.fn(),
  createSessionInWorkspace: vi.fn(() => 'sid-1'),
  isDraftSession: vi.fn(() => false),
  getSessionAgentDisplay: vi.fn(() => null),
}));

function chunk(kind: ChatChunk['kind'], requestId: string, body: Record<string, unknown> = {}): ChatChunk {
  return {
    kind,
    body,
    is_final: kind === 'final' || kind === 'error',
    sequence: 1,
    request_id: requestId,
    session_id: 'sid-1',
  };
}

function openTurn(requestId: string): Bookkeeping {
  ensureSessionBook('sid-1');
  patchBook('sid-1', {
    turnSealed: false,
    activeRequestId: requestId,
    acceptingNewRequest: false,
  });
  return sessionStore.get().books['sid-1']!;
}

beforeEach(() => {
  vi.clearAllMocks();
  __resetAllStoresForTest();
  clearFoldMemoryCache();
  window.localStorage.clear();
  setActiveSessionId('sid-1');
  document.body.innerHTML = `
    <div id="welcome-panel"></div>
    <div id="chat-panel" hidden><div id="chat-messages"></div></div>
    <div class="chat-todo-slot"></div>
    <div id="composer-controls"></div>
    <div class="chat-running-intro"></div>
  `;
});

afterEach(() => {
  _resetTurnDurationTickerForTests();
  vi.useRealTimers();
});

describe('applyChunk turn identity gate', () => {
  it('每秒刷新等待时间时保留同一个 spinner DOM 节点', () => {
    vi.useFakeTimers();
    appendSessionMessage('sid-1', {
      id: 'turn-live',
      role: 'assistant',
      content: '',
      thinking: '正在分析',
      timestamp: Date.now(),
      turnStartedAt: Date.now() - 3_000,
      streaming: true,
    });

    renderChat();
    const spinner = document.querySelector('.msg__fold-spinner');
    expect(spinner).not.toBeNull();

    vi.advanceTimersByTime(1_000);

    expect(document.querySelector('.msg__fold-spinner')).toBe(spinner);
    expect(document.querySelector('.msg__fold-label')?.textContent).toContain('已等待 4s');
  });

  it('连续 thinking 增量安全拼接且不重建 spinner', async () => {
    vi.useFakeTimers();
    openTurn('req-thinking');
    patchBook('sid-1', { assistantId: 'turn-thinking' });
    appendSessionMessage('sid-1', {
      id: 'turn-thinking',
      role: 'assistant',
      content: '',
      thinking: '第一段思考',
      timestamp: Date.now(),
      turnStartedAt: Date.now() - 1_000,
      streaming: true,
    });
    renderChat();
    const spinner = document.querySelector('.msg__fold-spinner');

    applyChunk(chunk('thinking', 'req-thinking', { text: '第二段思考' }));
    await vi.advanceTimersByTimeAsync(20);

    expect(document.querySelector('.msg__fold-spinner')).toBe(spinner);
    expect(document.querySelector('.process-timeline__thinking')?.textContent).toBe('第一段思考第二段思考');
  });

  it('连续正文分片只更新正文并保留气泡、头像和模型信息节点', async () => {
    vi.useFakeTimers();
    openTurn('req-delta-stable');
    patchBook('sid-1', { assistantId: 'turn-delta-stable' });
    appendSessionMessage('sid-1', {
      id: 'turn-delta-stable',
      role: 'assistant',
      content: '第一段正文',
      model: 'deepseek-v4-flash-ark',
      timestamp: Date.now(),
      turnStartedAt: Date.now() - 1_000,
      streaming: true,
      segmentRole: 'answer',
    });
    renderChat();
    const bubble = document.querySelector('.msg[data-streaming="true"]');
    const avatar = bubble?.querySelector('.msg__avatar');
    const model = bubble?.querySelector('.msg__meta');

    applyChunk(chunk('delta', 'req-delta-stable', { text: '第二段正文' }));
    await vi.advanceTimersByTimeAsync(20);

    expect(document.querySelector('.msg[data-streaming="true"]')).toBe(bubble);
    expect(document.querySelector('.msg[data-streaming="true"] .msg__avatar')).toBe(avatar);
    expect(document.querySelector('.msg[data-streaming="true"] .msg__meta')).toBe(model);
    expect(document.querySelector('.msg__text')?.textContent).toContain('第二段正文');
  });

  it('流式 thinking 在内部滚动区位于底部时继续跟随新增内容', async () => {
    vi.useFakeTimers();
    openTurn('req-thinking-scroll');
    patchBook('sid-1', { assistantId: 'turn-thinking-scroll' });
    appendSessionMessage('sid-1', {
      id: 'turn-thinking-scroll',
      role: 'assistant',
      content: '',
      thinking: '第一段思考',
      timestamp: Date.now(),
      turnStartedAt: Date.now() - 1_000,
      streaming: true,
    });
    renderChat();
    const thinking = document.querySelector<HTMLElement>('.process-timeline__thinking')!;
    Object.defineProperty(thinking, 'clientHeight', { configurable: true, value: 100 });
    Object.defineProperty(thinking, 'scrollHeight', {
      configurable: true,
      get: () => (thinking.textContent?.length ?? 0) * 20,
    });
    thinking.scrollTop = thinking.scrollHeight - thinking.clientHeight;

    applyChunk(chunk('thinking', 'req-thinking-scroll', { text: '第二段思考' }));
    await vi.advanceTimersByTimeAsync(20);

    expect(thinking.textContent).toBe('第一段思考第二段思考');
    expect(thinking.scrollTop).toBe(thinking.scrollHeight);
  });

  it('tool/generating 分片到达后立即渲染运行中时间线项（不等到 result）', () => {
    vi.useFakeTimers();
    openTurn('req-gen');
    appendSessionMessage('sid-1', {
      id: 'turn-gen',
      role: 'assistant',
      content: '',
      timestamp: Date.now(),
      turnStartedAt: Date.now() - 2_000,
      streaming: true,
    });
    patchBook('sid-1', { assistantId: 'turn-gen' });
    renderChat();

    applyChunk(chunk('tool', 'req-gen', {
      name: 'file_write',
      phase: 'generating',
      tool_call_id: 'call-1',
      args: '{"path":"/tmp/big.html"}',
      ui_label: '正在写入 big.html',
    }));
    // gate + reducer 必须当场接受 generating 帧并写回 toolCalls
    const msgs = messageStore.get().messages['sid-1']!;
    expect(msgs.some((m) => (m.toolCalls?.length ?? 0) > 0)).toBe(true);
    // rAF 合帧在 fake timers 下不可控，这里显式触发本帧渲染验证 DOM
    renderChat();

    const item = document.querySelector('.process-timeline__item');
    expect(item).not.toBeNull();
    expect(document.querySelector('.process-timeline__title')?.textContent).toBe('正在写入 big.html');
    expect(document.querySelector('.process-timeline__icon--running')).not.toBeNull();
    expect(document.querySelector('.process-timeline__duration[data-active]')).not.toBeNull();
  });

  it('工具实时读秒只原地更新文字，不按整秒重建 Timeline', () => {
    vi.useFakeTimers();
    const startedAt = new Date(2026, 6, 27, 12, 0, 0).getTime();
    vi.setSystemTime(startedAt + 10_000);
    appendSessionMessage('sid-1', {
      id: 'turn-tool-live',
      role: 'assistant',
      content: '',
      timestamp: startedAt,
      turnStartedAt: startedAt,
      streaming: true,
      toolCalls: [{
        toolCallId: 'call-live',
        name: 'file_read',
        status: 'running',
        startedAt,
      }],
    });

    renderChat();
    const item = document.querySelector('.process-timeline__item');
    const duration = document.querySelector<HTMLElement>('.process-timeline__duration[data-active]');
    expect(item).not.toBeNull();
    expect(duration).not.toBeNull();
    expect(updateActiveToolDurations(document, startedAt + 10_000)).toBe(1);
    expect(duration?.textContent).toBe('10s');
    const stableTextNode = duration?.firstChild;

    // 同一显示秒内不重复写 DOM；跨秒仍实时变更文字。
    expect(updateActiveToolDurations(document, startedAt + 10_200)).toBe(0);
    expect(duration?.firstChild).toBe(stableTextNode);
    expect(updateActiveToolDurations(document, startedAt + 11_000)).toBe(1);
    expect(duration?.textContent).toBe('11s');

    // 即便其他流式事件触发 renderChat，纯时间变化也不得替换 Timeline。
    vi.setSystemTime(startedAt + 12_000);
    renderChat();
    expect(document.querySelector('.process-timeline__item')).toBe(item);
  });

  it('session_title replaces the local user-input title', () => {
    sessionStore.set({
      sessions: [
        { id: 'sid-1', title: '今天周几?', updatedAt: 1, preview: '', badge: '主智能体', workspaceId: 'default' },
      ],
    });

    applyChunk(chunk('session_title', 'req-1', { title: '日期查询' }));

    expect(sessionStore.get().sessions[0]?.title).toBe('日期查询');
  });

  it('drops stale generation frames from a previous request before they touch stores', () => {
    openTurn('req-new');
    setSessionStatus('sid-1', 'running');

    applyChunk(chunk('delta', 'req-old', { text: 'stale' }));
    applyChunk(chunk('tool', 'req-old', { tool_call_id: 't-old', phase: 'start', name: 'search' }));
    applyChunk(chunk('status', 'req-old', { message: 'old running' }));

    expect(messageStore.get().messages['sid-1'] ?? []).toEqual([]);
    expect(sessionStore.get().books['sid-1']?.activeRequestId).toBe('req-new');
    expect(sessionStore.get().sessionStatuses['sid-1']).toBe('running');
  });

  it('drops late matching generation frames after final seals the request', () => {
    openTurn('req-1');

    applyChunk(chunk('delta', 'req-1', { text: 'answer' }));
    applyChunk(chunk('final', 'req-1', { text: 'answer' }));
    const afterFinalMessages = messageStore.get().messages['sid-1'] ?? [];

    applyChunk(chunk('delta', 'req-1', { text: ' late' }));
    applyChunk(chunk('tool', 'req-1', { tool_call_id: 't-late', phase: 'start', name: 'search' }));
    applyChunk(chunk('status', 'req-1', { message: 'late running' }));

    expect(messageStore.get().messages['sid-1']).toEqual(afterFinalMessages);
    expect(sessionStore.get().books['sid-1']?.turnSealed).toBe(true);
    expect(sessionStore.get().sessionStatuses['sid-1']).toBe('idle');
  });

  it('allows plan review and auxiliary updates after final', () => {
    openTurn('req-1');

    applyChunk(chunk('delta', 'req-1', { text: 'plan' }));
    applyChunk(chunk('final', 'req-1', { text: 'plan' }));
    applyChunk(chunk('plan_review', 'req-1', { plan: 'step 1', plan_file: 'plan.md' }));
    applyChunk(chunk('todo_updated', 'req-1', { todos: [{ id: 'todo-1', content: 'A', status: 'pending' }] }));

    expect(sessionStore.get().books['sid-1']?.pendingPlan).toEqual({ plan: 'step 1', planFile: 'plan.md', status: 'pending' });
    expect(sessionStore.get().books['sid-1']?.todos).toEqual([{ id: 'todo-1', content: 'A', status: 'pending' }]);
  });

  it('renders plan review entry card that opens the plan board instead of inline approve buttons', () => {
    const sid = 'sid-plan-card';
    setActiveSessionId(sid);
    ensureSessionBook(sid);
    appendSessionMessage(sid, {
      id: 'm-plan',
      role: 'assistant',
      content: '',
      timestamp: 1,
      planReview: {
        plan: '# Test Plan\n\n- Step 1',
        planFile: 'plans/test.md',
        status: 'pending',
        sessionId: sid,
      },
    });

    renderChat();

    expect((document.getElementById('welcome-panel') as HTMLElement).hidden).toBe(true);
    expect((document.getElementById('chat-panel') as HTMLElement).hidden).toBe(false);
    const card = document.querySelector('.plan-review-card') as HTMLElement | null;
    expect(card?.textContent).toContain('Test Plan');
    expect(card?.textContent).toContain('在看板中审阅');
    expect(card?.textContent).not.toContain('批准并执行');
    expect(card?.querySelector('[data-plan-action="open_board"]')).toBeTruthy();
  });

  it('readonly plan review stays entry-only without plan body in chat', () => {
    const sid = 'sid-plan-fold';
    setActiveSessionId(sid);
    ensureSessionBook(sid);
    appendSessionMessage(sid, {
      id: 'm-plan-fold',
      role: 'assistant',
      content: '',
      timestamp: 1,
      streaming: true,
      planReview: {
        plan: '# Fold Plan\n\n- Keep open state\n- Secret body line XYZ',
        planFile: 'plans/fold.md',
        status: 'readonly',
        sessionId: sid,
      },
    });

    renderChat();
    const card = document.querySelector('.plan-review-card') as HTMLElement | null;
    expect(card?.classList.contains('plan-review-card--entry')).toBe(true);
    expect(card?.textContent).toContain('Fold Plan');
    expect(card?.textContent).toContain('在看板中查看');
    expect(card?.textContent).not.toContain('Secret body line XYZ');
    expect(card?.querySelector('.plan-review-card__content')).toBeNull();

    // 流式重渲染后仍保持入口卡、不展开正文
    const msgs = messageStore.get().messages[sid] ?? [];
    messageStore.set({
      messages: {
        ...messageStore.get().messages,
        [sid]: msgs.map((m) => (m.id === 'm-plan-fold' ? { ...m, content: '正在执行…' } : m)),
      },
    });
    renderChat();

    const card2 = document.querySelector('.plan-review-card') as HTMLElement | null;
    expect(card2?.querySelector('.plan-review-card__content')).toBeNull();
    expect(card2?.textContent).not.toContain('Secret body line XYZ');
    expect(card2?.querySelector('[data-plan-action="open_board"]')).toBeTruthy();
  });

  it('renders todo progress panel in the fixed todo slot', () => {
    openTurn('req-todo-card');
    applyChunk(chunk('todo_updated', 'req-todo-card', {
      todos: [{ id: 'todo-1', content: 'Write tests', status: 'in_progress' }],
    }));

    renderChat();

    expect((document.getElementById('welcome-panel') as HTMLElement).hidden).toBe(true);
    expect((document.getElementById('chat-panel') as HTMLElement).hidden).toBe(false);
    expect(document.querySelector('.chat-todo-slot .desktop-todo-panel')?.textContent).toContain('Write tests');
  });

  it('drops stale request-scoped auxiliary frames from a previous request', () => {
    openTurn('req-new');

    applyChunk(chunk('plan_review', 'req-old', { plan: 'old plan', plan_file: 'old.md' }));
    applyChunk(chunk('todo_updated', 'req-old', { todos: [{ id: 'old', content: 'old', status: 'pending' }] }));
    applyChunk(chunk('file_changes', 'req-old', { files: [{ path: 'old.txt', added: 1, removed: 0, status: 'added', diff: [] }] }));

    expect(sessionStore.get().books['sid-1']?.pendingPlan).toBeNull();
    expect(sessionStore.get().books['sid-1']?.todos).toEqual([]);
    expect(sessionStore.get().books['sid-1']?.fileChanges).toEqual([]);
  });

  it('collapses the completed execution process when plan review arrives', () => {
    openTurn('req-plan');

    applyChunk(chunk('tool', 'req-plan', { tool_call_id: 't-plan', phase: 'start', name: 'file_write' }));
    const turnId = messageStore.get().messages['sid-1']?.[0]?.id;
    expect(turnId).toBeTruthy();
    state.userUnfoldedTurns.add(turnId!);

    applyChunk(chunk('final', 'req-plan', { text: '计划正文' }));
    applyChunk(chunk('plan_review', 'req-plan', { plan: '计划正文', plan_file: 'plan.md' }));

    expect(state.userUnfoldedTurns.has(turnId!)).toBe(false);
    expect(state.userFoldedTurns.has(turnId!)).toBe(true);
    expect(sessionStore.get().books['sid-1']?.pendingPlan).toEqual({
      plan: '计划正文',
      planFile: 'plan.md',
      status: 'pending',
    });
  });

  it('ignores programmatic fold toggles and persists user initiated fold toggles', () => {
    const sid = 'sid-fold-intent';
    setActiveSessionId(sid);
    ensureSessionBook(sid);
    appendSessionMessage(sid, {
      id: 'turn-programmatic',
      role: 'assistant',
      content: '正文',
      thinking: '过程',
      timestamp: 1,
      streaming: false,
    });

    renderChat();
    const details = document.querySelector<HTMLDetailsElement>('details.msg__foldable');
    expect(details).not.toBeNull();

    details!.open = true;
    details!.dispatchEvent(new Event('toggle'));
    expect(state.userUnfoldedTurns.has('turn-programmatic')).toBe(false);
    expect(state.userFoldedTurns.has('turn-programmatic')).toBe(false);

    details!.dispatchEvent(new Event('pointerdown', { bubbles: true }));
    details!.open = false;
    details!.dispatchEvent(new Event('toggle'));
    expect(state.userUnfoldedTurns.has('turn-programmatic')).toBe(false);
    expect(state.userFoldedTurns.has('turn-programmatic')).toBe(true);
  });

  it('accepts control status without changing running state', () => {
    openTurn('req-1');
    setSessionStatus('sid-1', 'idle');

    applyChunk({
      kind: 'status',
      body: { message: '已停止', control: true },
      is_final: false,
      sequence: 1,
      session_id: 'sid-1',
    });

    expect(messageStore.get().messages['sid-1']?.[0]?.role).toBe('status');
    expect(sessionStore.get().sessionStatuses['sid-1']).toBe('idle');
    expect(sessionStore.get().busySessions['sid-1']).toBeUndefined();
  });
});
