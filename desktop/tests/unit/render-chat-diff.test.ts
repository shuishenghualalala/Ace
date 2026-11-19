/**
 * 流式渲染纯逻辑单测。
 * 覆盖 applyFoldState（toggle 事件委托）+ createChatRenderCoalescer（渲染合并）。
 *
 * X3a（2026-06）：补充 chat-render 导出的纯逻辑 helper 单测
 * （formatMessageTime / formatDuration / sessionStatusClass）。
 *
 * 对话面板多实例化重构（2026-08）：环境切到 happy-dom，末尾补
 * conversation-renderer 双实例（不同 containerId/sessionId）隔离用例。
 */
// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, it, expect, vi } from 'vitest';
import {
  applyFoldState,
  createChatRenderCoalescer,
  createStreamingPatchCoalescer,
  type FoldSets,
} from '../../src/ui/render-utils';
import { formatMessageTime, formatDuration, renderQueuePanelHtml, sessionStatusClass, type ChatMessage } from '../../src/ui/chat-render';
import {
  disposeConversationRenderer,
  getConversationScrollAnchor,
  invalidateConversationFold,
  preserveConversationHistoryViewport,
  restoreConversationViewport,
  revealConversationMessage,
  renderConversation,
} from '../../src/ui/features/conversation-renderer';
import { __resetAllStoresForTest } from '../../src/ui/stores/stores';
import {
  appendSessionMessage,
  clearHistoryLoadError,
  ensureSessionMessages,
  markHistoryLoadError,
  replaceSessionMessages,
  setEditFrom,
  setHistoryLoading,
  state,
} from '../../src/ui/state';
import { applyMessageUpserts, type MessageUpsert } from '../../src/ui/reducers/chat-reducer';
import { emptyFeatureReducerResult } from '../../src/ui/features/event-reducer-registry';
import nativeFixture from '../../../tests/fixtures/native-visual.json';
import { reportRendererError } from '../../src/ui/renderer-error-report';

// conversation-renderer 的重依赖：本文件只验证消息流渲染与 diff 缓存隔离，
// 外部会话身份 / 看板 / 浏览器面板点击跳转不在此覆盖。
vi.mock('../../src/ui/features/workspaces', () => ({
  getSessionAgentDisplay: vi.fn(() => null),
}));
vi.mock('../../src/ui/features/inspector', () => ({
  openBrowserWorkbench: vi.fn(),
  openInspectorToTab: vi.fn(),
}));
vi.mock('../../src/ui/features/browser-panel', () => ({
  openBrowserArtifact: vi.fn(async () => 'in_app'),
  openUserBrowser: vi.fn(async () => 'in_app'),
}));

// P0-1 渲染隔离：只毒化这两个 id 的消息构建，其余走真实实现。
// poison-user → 仅 build 抛错；poison-sig → sig 与 build 都抛错（坏字段形状）。
vi.mock('../../src/ui/chat-render', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../src/ui/chat-render')>();
  return {
    ...actual,
    renderMessageHtml: vi.fn((msg: { id?: string }) => {
      if (msg?.id === 'poison-user' || msg?.id === 'poison-sig') throw new Error('poisoned build');
      return actual.renderMessageHtml(msg as Parameters<typeof actual.renderMessageHtml>[0]);
    }),
  };
});
// 只替换上报入口（断言用），其余（safeStringify 等）走真实实现。
vi.mock('../../src/ui/renderer-error-report', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../src/ui/renderer-error-report')>();
  return { ...actual, reportRendererError: vi.fn() };
});

describe('applyFoldState', () => {
  function makeSets(): FoldSets {
    return { unfolded: new Set<string>(), folded: new Set<string>() };
  }

  it('open/close 状态在两个集合间往返迁移', () => {
    const sets = makeSets();
    sets.folded.add('t1');
    applyFoldState('t1', true, sets);
    expect(sets.unfolded.has('t1')).toBe(true);
    expect(sets.folded.has('t1')).toBe(false);

    applyFoldState('t1', false, sets);
    expect(sets.folded.has('t1')).toBe(true);
    expect(sets.unfolded.has('t1')).toBe(false);

    applyFoldState('t1', true, sets);
    expect(Array.from(sets.folded)).toEqual([]);
    expect(Array.from(sets.unfolded)).toEqual(['t1']);
  });
});

describe('createChatRenderCoalescer', () => {
  it('coalesces multiple schedule() in the same window into one render', () => {
    const queue: Array<() => void> = [];
    const scheduler = (cb: () => void) => queue.push(cb);
    let renderCalls = 0;
    const schedule = createChatRenderCoalescer(() => { renderCalls += 1; }, scheduler);

    // 模拟一帧内到达 30 个 delta chunk → schedule 30 次
    for (let i = 0; i < 30; i += 1) schedule();
    expect(queue.length).toBe(1); // 只排了一个回调
    expect(renderCalls).toBe(0); // 还没 flush

    // flush 该帧
    queue.splice(0).forEach((fn) => fn());
    expect(renderCalls).toBe(1); // 整帧只渲染一次
  });

  it('after a window flushes, the next schedule renders again', () => {
    const queue: Array<() => void> = [];
    const scheduler = (cb: () => void) => queue.push(cb);
    let renderCalls = 0;
    const schedule = createChatRenderCoalescer(() => { renderCalls += 1; }, scheduler);

    schedule();
    queue.splice(0).forEach((fn) => fn());
    expect(renderCalls).toBe(1);

    schedule();
    queue.splice(0).forEach((fn) => fn());
    expect(renderCalls).toBe(2);
  });

  it('no schedule → no render', () => {
    const scheduler = (_cb: () => void) => { /* never flushes */ };
    let renderCalls = 0;
    const schedule = createChatRenderCoalescer(() => { renderCalls += 1; }, scheduler);
    // 不调用 schedule
    void schedule;
    expect(renderCalls).toBe(0);
  });

  it('cancel before flush suppresses the queued render', () => {
    const queue: Array<() => void> = [];
    const scheduler = (cb: () => void) => queue.push(cb);
    let renderCalls = 0;
    const schedule = createChatRenderCoalescer(() => { renderCalls += 1; }, scheduler);

    schedule();
    schedule.cancel();
    queue.splice(0).forEach((fn) => fn());
    expect(renderCalls).toBe(0);
  });

  it('cancel then reschedule keeps the old callback from consuming the new render', () => {
    const queue: Array<() => void> = [];
    const scheduler = (cb: () => void) => queue.push(cb);
    let renderCalls = 0;
    const schedule = createChatRenderCoalescer(() => { renderCalls += 1; }, scheduler);

    schedule();
    schedule.cancel();
    schedule();
    expect(queue).toHaveLength(2);
    queue.shift()?.();
    expect(renderCalls).toBe(0);
    queue.shift()?.();
    expect(renderCalls).toBe(1);
  });
});

describe('createStreamingPatchCoalescer', () => {
  it('coalesces patch targets and applies only the latest one per window', () => {
    const queue: Array<() => void> = [];
    const scheduler = (cb: () => void) => queue.push(cb);
    const patched: string[] = [];
    const coalescer = createStreamingPatchCoalescer(
      (target) => patched.push(`${target.sid}:${target.assistantId}`),
      scheduler,
    );

    coalescer.schedule({ sid: 's1', assistantId: 'a1' });
    coalescer.schedule({ sid: 's1', assistantId: 'a2' });

    expect(queue.length).toBe(1);
    queue.splice(0).forEach((fn) => fn());
    expect(patched).toEqual(['s1:a2']);
  });

  it('clear drops a pending patch', () => {
    const queue: Array<() => void> = [];
    const scheduler = (cb: () => void) => queue.push(cb);
    const patched: string[] = [];
    const coalescer = createStreamingPatchCoalescer(
      (target) => patched.push(`${target.sid}:${target.assistantId}`),
      scheduler,
    );

    coalescer.schedule({ sid: 's1', assistantId: 'a1' });
    coalescer.clear();
    queue.splice(0).forEach((fn) => fn());
    expect(patched).toEqual([]);
  });
});

describe('formatMessageTime', () => {
  it('returns empty string for falsy timestamp (0)', () => {
    expect(formatMessageTime(0)).toBe('');
  });

  it('formats a timestamp as HH:MM with zero-padding', () => {
    // formatMessageTime uses local time (new Date(ts).getHours()); build a local
    // date so the assertion is timezone-independent.
    const d = new Date(2024, 0, 15, 13, 5, 0);
    const out = formatMessageTime(d.getTime());
    expect(out).toMatch(/^\d{2}:\d{2}$/);
    expect(out.length).toBe(5);
  });

  it('pads single-digit hours and minutes', () => {
    const d = new Date(2024, 0, 15, 1, 2, 0);
    expect(formatMessageTime(d.getTime())).toBe('01:02');
  });
});

describe('formatDuration', () => {
  it('sub-second durations use decimal seconds', () => {
    expect(formatDuration(0)).toBe('0.0s');
    expect(formatDuration(500)).toBe('0.5s');
    expect(formatDuration(999)).toBe('1.0s');
  });

  it('1s..59s → whole seconds', () => {
    expect(formatDuration(1000)).toBe('1s');
    expect(formatDuration(59000)).toBe('59s');
  });

  it('1m..59m → minutes (with seconds when non-zero)', () => {
    expect(formatDuration(60000)).toBe('1m');
    expect(formatDuration(100000)).toBe('1m 40s');
    expect(formatDuration(3540000)).toBe('59m');
  });

  it('>=1h → hours (with minutes when non-zero)', () => {
    expect(formatDuration(3600000)).toBe('1h');
    expect(formatDuration(3900000)).toBe('1h 5m');
    expect(formatDuration(7200000)).toBe('2h');
  });

  it('clamps negative input to 0', () => {
    expect(formatDuration(-500)).toBe('0.0s');
    expect(formatDuration(-60000)).toBe('0.0s');
  });
});

describe('sessionStatusClass', () => {
  it('empty for undefined or idle', () => {
    expect(sessionStatusClass(undefined)).toBe('');
    expect(sessionStatusClass('idle')).toBe('');
  });

  it('suffixes non-idle statuses', () => {
    expect(sessionStatusClass('running')).toBe(' history-item--running');
    expect(sessionStatusClass('queued')).toBe(' history-item--queued');
    expect(sessionStatusClass('error')).toBe(' history-item--error');
  });
});

describe('renderQueuePanelHtml', () => {
  it('renders compact queued message actions and escapes user text', () => {
    const html = renderQueuePanelHtml([{ id: 'q1', query: 'hello <script>' }], true);

    expect(html).toContain('data-queue-steer="0"');
    expect(html).toContain('>引导<');
    expect(html).toContain('data-queue-edit="0"');
    expect(html).toContain('data-queue-menu="0"');
    expect(html).toContain('data-queue-move="0"');
    expect(html).toContain('等待队列第 1 条');
    expect(html).toContain('编辑消息');
    expect(html).toContain('上移');
    expect(html).toContain('下移');
    expect(html).toContain('>删除<');
    expect(html).toContain('hello &lt;script&gt;');
    expect(html).not.toContain('当前任务结束后自动发送');
  });

  it('hides steer when the session cannot accept steer', () => {
    const html = renderQueuePanelHtml([{ id: 'q1', query: 'queued' }], false);

    expect(html).not.toContain('data-queue-steer');
    expect(html).toContain('data-queue-menu="0"');
  });
});

describe('conversation-renderer 双实例隔离', () => {
  beforeEach(() => {
    __resetAllStoresForTest();
    document.body.innerHTML = '<div id="panel-a"></div><div id="panel-b"></div>';
  });

  afterEach(() => {
    disposeConversationRenderer('panel-a');
    disposeConversationRenderer('panel-b');
  });

  it.each([false, true])('renders and updates a message containing invalid math (streaming=%s)', (streaming) => {
    vi.mocked(reportRendererError).mockClear();
    const panel = document.getElementById('panel-a')!;
    const code = "$('#emptyState').hidden = true; $('#timeline').innerHTML = items;";
    const content = `**页面更新**\n\n${code}\n\n正常公式 $E=mc^2$\n\n$# <img src=x onerror=alert(1)>$`;
    const message: ChatMessage = { id: 'math-fallback', role: 'assistant', content, timestamp: 1, streaming };
    replaceSessionMessages('sid-math', [message]);
    renderConversation(panel, 'panel-a', 'sid-math');
    const text = panel.querySelector('[data-text-for="math-fallback"]')!;
    expect(text.textContent).toContain(code);
    expect(text.querySelector('strong')?.textContent).toBe('页面更新');
    expect(text.querySelector('.katex')).not.toBeNull();
    expect(text.querySelector('img')).toBeNull();
    expect(panel.textContent).not.toContain('该消息渲染失败');
    expect(reportRendererError).not.toHaveBeenCalled();

    replaceSessionMessages('sid-math', [{ ...message, content: `${content}\n\n后续内容继续显示。` }]);
    renderConversation(panel, 'panel-a', 'sid-math');
    expect(panel.textContent).toContain('后续内容继续显示。');
    expect(panel.textContent).toContain(code);
    expect(reportRendererError).not.toHaveBeenCalled();
  });

  it('does not render legacy inline HTML snapshots', () => {
    const panel = document.getElementById('panel-a')!;
    appendSessionMessage('sid-a', { id: 'answer', role: 'assistant', content: '图表', timestamp: 1,
      toolCalls: [{ toolCallId: 'widget-show', name: 'Widget', status: 'done', startedAt: 0,
        result: JSON.stringify({ ok: true, surface: { display: 'inline', version: 1, mode: 'widget',
          sessionId: 'sid-a', widgetId: 'widget_123456789abc', title: '收益', status: 'ready', html: '<p>chart</p>' } }) }] });
    renderConversation(panel, 'panel-a', 'sid-a');
    expect(panel.querySelector('iframe')).toBeNull();
    expect(panel.querySelector('.inline-widget')).toBeNull();
  });

  it('updates live terminal output on a cached turn and replaces it with the completed response', () => {
    const panel = document.getElementById('panel-a')!;
    const tool = { toolCallId: 'terminal-1', name: 'terminal', status: 'running' as const, startedAt: 1 };
    const message: ChatMessage = {
      id: 'terminal-turn', role: 'assistant', content: '', timestamp: 1, streaming: true,
    };
    const render = (tools: ChatMessage['toolCalls'], streaming = true) => {
      replaceSessionMessages('sid-terminal', [{ ...message, toolCalls: tools, streaming }]);
      renderConversation(panel, 'panel-a', 'sid-terminal');
    };
    render([{ ...tool, terminalProgress: { text: ' 雪𠮷 ', clipped: false } }]);
    expect(panel.querySelector('[data-terminal-live-output]')?.textContent).toBe(' 雪𠮷 ');
    render([{ ...tool, terminalProgress: { text: ' 雪𠮷 <child>\n', clipped: true } }]);
    const live = panel.querySelector('[data-terminal-live-output]')!;
    expect(live.textContent).toBe(' 雪𠮷 <child>\n');
    expect(live.querySelector('child')).toBeNull();
    expect(live.parentElement?.textContent).toMatch(/早期输出已省略|earlier output omitted/);
    expect(panel.querySelector('[data-section="result"]')).toBeNull();

    render([{ ...tool, status: 'done', result: '雪𠮷 finished' }], false);
    expect(panel.querySelector('[data-terminal-live-output]')).toBeNull();
    expect(panel.querySelector('[data-section="result"] pre')?.textContent).toBe('雪𠮷 finished');
  });

  it('keeps native controls and their values while the answer streams', () => {
    const panel = document.getElementById('panel-a')!;
    appendSessionMessage('sid-a', { id: 'native-answer', role: 'assistant', content: '解释', timestamp: 1,
      toolCalls: [{ toolCallId: 'native-render', name: 'Widget', status: 'done', startedAt: 0,
        result: JSON.stringify({ ok: true, surface: { display: 'inline', version: 3, mode: 'native',
          sessionId: 'sid-a', status: 'ready', spec: nativeFixture } }) }] });
    renderConversation(panel, 'panel-a', 'sid-a');
    const input = panel.querySelector<HTMLInputElement>('[name="income"]')!;
    expect(input).not.toBeNull(); input.value = '12000'; input.dispatchEvent(new Event('input'));
    appendSessionMessage('sid-a', { id: 'native-continuation', role: 'assistant', content: '继续解释', timestamp: 2 });
    renderConversation(panel, 'panel-a', 'sid-a');
    expect(panel.querySelector('[name="income"]')).toBe(input); expect(input.value).toBe('12000');
    expect(panel.querySelector('iframe')).toBeNull();
  });

  it('不同 containerId/sessionId 的 diff 缓存与 scroll anchor 互不干扰', () => {
    const panelA = document.getElementById('panel-a')!;
    const panelB = document.getElementById('panel-b')!;
    appendSessionMessage('sid-a', { id: 'u-a', role: 'user', content: '面板A消息', timestamp: 1 });
    appendSessionMessage('sid-b', { id: 'u-b', role: 'user', content: '面板B消息', timestamp: 1 });

    renderConversation(panelA, 'panel-a', 'sid-a');
    renderConversation(panelB, 'panel-b', 'sid-b');
    expect(panelA.textContent).toContain('面板A消息');
    expect(panelA.textContent).not.toContain('面板B消息');
    expect(panelB.textContent).toContain('面板B消息');
    expect(panelB.textContent).not.toContain('面板A消息');
    // scroll anchor 按容器 id 分键：两个实例各自独立
    expect(getConversationScrollAnchor('panel-a')).not.toBe(getConversationScrollAnchor('panel-b'));

    // A 追加消息重渲：B 的 DOM 节点身份不变（各自的 diff 缓存互不影响）
    const bFirst = panelB.querySelector('.msg');
    const wrapper = panelA.querySelector('.messages__inner')!;
    const history = wrapper.firstChild;
    const mutations = new MutationObserver(() => {});
    mutations.observe(wrapper, { childList: true });
    appendSessionMessage('sid-a', { id: 'u-a2', role: 'user', content: '面板A第二条', timestamp: 2 });
    renderConversation(panelA, 'panel-a', 'sid-a');
    expect(mutations.takeRecords().some((record) => Array.from(record.removedNodes).includes(history!))).toBe(false);
    expect(wrapper.firstChild).toBe(history);
    mutations.disconnect();
    expect(panelA.textContent).toContain('面板A第二条');
    expect(panelB.querySelector('.msg')).toBe(bFirst);

    // A 切到 B 的会话：只重置 A 容器的缓存；B 的 DOM 仍保持原节点
    renderConversation(panelA, 'panel-a', 'sid-b');
    expect(panelA.textContent).toContain('面板B消息');
    expect(panelA.textContent).not.toContain('面板A第二条');
    expect(panelB.querySelector('.msg')).toBe(bFirst);
  });
});

describe('conversation-renderer tracked history and live changes', () => {
  const sessionId = 'tracked-history';
  const containerId = 'tracked-panel';
  let panel: HTMLElement;

  function row(id: string, role: ChatMessage['role'], content = id): ChatMessage {
    return { id, role, content, timestamp: 1 };
  }

  function render(): void {
    renderConversation(panel, containerId, sessionId);
  }

  function upsert(...messageUpserts: MessageUpsert[]): void {
    const previous = ensureSessionMessages(sessionId);
    replaceSessionMessages(sessionId, applyMessageUpserts(previous, {
      ...emptyFeatureReducerResult(), messageUpserts,
    }));
  }

  beforeEach(() => {
    __resetAllStoresForTest();
    panel = document.createElement('div');
    panel.id = containerId;
    document.body.replaceChildren(panel);
  });

  afterEach(() => {
    document.getSelection()?.removeAllRanges();
    disposeConversationRenderer(containerId);
  });

  it('coalesces tracked live updates without revisiting historical attachments or tool calls', () => {
    let historyAttachmentReads = 0;
    let historyToolReads = 0;
    replaceSessionMessages(sessionId, [
      {
        ...row('hist-user', 'user'),
        get attachments() { historyAttachmentReads += 1; return []; },
      },
      {
        ...row('hist-answer', 'assistant'),
        get toolCalls() { historyToolReads += 1; return []; },
      },
      row('new-user', 'user'),
      { ...row('live-answer', 'assistant', 'First live words'), streaming: true },
    ]);
    render();
    const historyNode = panel.querySelector('.msg[data-message-id="hist-answer"]')!;
    expect(historyNode.isConnected).toBe(true);
    expect(historyAttachmentReads).toBeGreaterThan(0);
    expect(historyToolReads).toBeGreaterThan(0);
    historyAttachmentReads = 0;
    historyToolReads = 0;

    upsert({ op: 'patch', messageId: 'live-answer', patch: { content: 'Updated live words' } });
    upsert({ op: 'patch', messageId: 'live-answer', patch: { content: 'Latest coalesced live words' } });
    render();

    expect(panel.querySelector('[data-text-for="live-answer"]')?.textContent).toContain('Latest coalesced live words');
    expect(panel.querySelector('.msg[data-message-id="hist-answer"]')).toBe(historyNode);
    expect(historyAttachmentReads).toBe(0);
    expect(historyToolReads).toBe(0);
  });

  it('merges a replayed earlier row across its old standalone boundary', () => {
    replaceSessionMessages(sessionId, [
      row('first-user', 'user'), row('first-answer', 'assistant'),
      row('old-boundary', 'user'), row('replayed-answer', 'assistant'),
      row('later-user', 'user'), row('later-answer', 'assistant'),
    ]);
    render();
    expect(panel.querySelector('.msg[data-message-id="replayed-answer"]')).not.toBeNull();

    upsert({ op: 'patch', messageId: 'old-boundary', patch: { role: 'status', content: 'Recovered tool status' } });
    render();

    const merged = panel.querySelector('.msg[data-message-id="first-answer"]')!;
    expect(merged.querySelector('[data-text-for="replayed-answer"]')?.textContent).toBe('replayed-answer');
    expect(merged.querySelector('[data-message-id="old-boundary"]')?.textContent).toContain('Recovered tool status');
    expect(panel.querySelector('.msg[data-message-id="replayed-answer"]')).toBeNull();
    expect(panel.querySelector('.msg[data-message-id="later-answer"]')).not.toBeNull();
  });

  it('prepends an older page without detaching the previous reading row or selection', () => {
    const visibleHistory = [row('selected', 'user', 'Select this historical message'), row('reply', 'assistant')];
    replaceSessionMessages(sessionId, visibleHistory);
    render();
    const wrapper = panel.querySelector('.messages__inner')!;
    const readingRow = panel.querySelector<HTMLElement>('.msg[data-message-id="selected"]')!;
    const walker = document.createTreeWalker(readingRow, NodeFilter.SHOW_TEXT);
    let textNode = walker.nextNode();
    while (textNode && !textNode.textContent?.includes('Select this historical message')) textNode = walker.nextNode();
    expect(textNode).not.toBeNull();
    const range = document.createRange();
    range.setStart(textNode!, 0);
    range.setEnd(textNode!, 6);
    const selection = document.getSelection()!;
    selection.addRange(range);
    const mutations = new MutationObserver(() => {});
    mutations.observe(wrapper, { childList: true });

    replaceSessionMessages(sessionId, [row('older-user', 'user'), row('older-reply', 'assistant'), ...visibleHistory]);
    render();

    expect(wrapper.querySelector('.msg')?.getAttribute('data-message-id')).toBe('older-user');
    expect(wrapper.querySelector('.msg[data-message-id="selected"]')).toBe(readingRow);
    expect(mutations.takeRecords().some(({ removedNodes }) => Array.from(removedNodes).includes(readingRow))).toBe(false);
    expect(selection.getRangeAt(0).startContainer).toBe(textNode);
    expect(selection.toString()).toBe('Select');
    mutations.disconnect();
  });

  it('invalidates an earlier Widget visual when a tracked later turn supersedes its revision', () => {
    function widget(id: string, revision: number): ChatMessage {
      return {
        ...row(id, 'assistant', `Widget response ${revision}`),
        toolCalls: [{
          toolCallId: `call-${id}`, name: 'Widget', status: 'done', startedAt: 1,
          result: JSON.stringify({ ok: true, surface: {
            version: 3, mode: 'native', display: 'inline', status: 'ready', sessionId,
            spec: { ...nativeFixture, revision },
          } }),
        }],
      };
    }
    replaceSessionMessages(sessionId, [row('first-user', 'user'), widget('first-widget', 1), row('later-user', 'user')]);
    render();
    const oldVisual = panel.querySelector('.native-visual[data-revision="1"]')!;
    expect(oldVisual.textContent).not.toContain('历史快照');

    appendSessionMessage(sessionId, widget('current-widget', 2));
    render();

    expect(panel.querySelector('.native-visual[data-revision="1"]')?.textContent).toContain('历史快照 · 修订 1');
    const currentVisual = panel.querySelector('.native-visual[data-revision="2"]')!;
    expect(currentVisual).not.toBeNull();
    expect(currentVisual.textContent).not.toContain('历史快照');
  });

  it('keeps the rewritten user outcome separate, keyed and attached to its original prompt', () => {
    const original = {
      ...row('user-original', 'user'), requestId: 'req-a', historyId: 'durable-original',
      turnOutcome: { status: 'failed' as const, code: 'auth' },
    };
    const later = row('user-later', 'user');
    replaceSessionMessages(sessionId, [original, later]);
    render();

    const first = panel.querySelector('[data-message-id="user-original"]')!;
    const marker = first.nextElementSibling!;
    expect(first.querySelector('[data-turn-outcome]')).toBeNull();
    expect(marker.getAttribute('data-message-id')).toBe('user-outcome-durable-original');
    expect(marker.querySelector('[data-turn-outcome="failed"]')).not.toBeNull();
    expect(marker.nextElementSibling?.getAttribute('data-message-id')).toBe('user-later');

    replaceSessionMessages(sessionId, [{ ...original }, later]);
    render();
    expect(panel.querySelector('[data-message-id="user-outcome-durable-original"]')).toBe(marker);
    expect(panel.querySelectorAll('[data-turn-outcome="failed"]')).toHaveLength(1);
  });

  it('reconciles same-length authoritative history and handles edit and fold changes without message updates', () => {
    const user = row('first-user', 'user');
    const answer = { ...row('first-answer', 'assistant'), thinking: 'Previously completed thought' };
    replaceSessionMessages(sessionId, [user, answer, row('later-user', 'user')]);
    render();
    const wrapper = panel.querySelector('.messages__inner')!;
    const userNode = wrapper.querySelector('.msg[data-message-id="first-user"]');

    replaceSessionMessages(sessionId, [user, { ...answer, content: 'Authoritative correction' }, row('later-user', 'user')]);
    render();
    expect(panel.textContent).toContain('Authoritative correction');
    expect(panel.querySelector('.msg[data-message-id="first-user"]')).toBe(userNode);

    const firstAnswerDetails = () => panel.querySelector<HTMLDetailsElement>('.msg[data-message-id="first-answer"] details.msg__foldable');
    expect(firstAnswerDetails()?.open).toBe(false);
    state.userUnfoldedTurns.add('first-answer');
    invalidateConversationFold(sessionId, 'first-answer');
    render();
    expect(firstAnswerDetails()?.open).toBe(true);

    setEditFrom(sessionId, 2);
    render();
    expect(panel.querySelector('.msg[data-message-id="later-user"]')).toBeNull();
    expect(firstAnswerDetails()?.open).toBe(true);

    setEditFrom(sessionId, null);
    render();
    expect(panel.querySelector('.msg[data-message-id="later-user"]')).not.toBeNull();
  });

  function longHistory(count: number): ChatMessage[] {
    return Array.from({ length: count }, (_, index) => row(
      'history-' + index, index % 2 === 0 ? 'user' : 'assistant',
    ));
  }

  function mountedRows(): number {
    return panel.querySelectorAll('.messages__inner > [data-chat-history-row]').length;
  }

  function simulateViewportGeometry(): void {
    const wrapper = panel.querySelector<HTMLElement>('.messages__inner')!;
    wrapper.getBoundingClientRect = () => new DOMRect(0, -panel.scrollTop, 800, 0);
  }

  it('keeps a bounded window and the live tail, and preserves selected/focused rows through a distant reveal', () => {
    Object.defineProperty(panel, 'clientHeight', { configurable: true, value: 480 });
    replaceSessionMessages(sessionId, longHistory(400));
    render();
    simulateViewportGeometry();
    expect(panel.classList.contains('conversation-viewport--active')).toBe(true);
    expect(panel.querySelector('[data-message-id="history-0"]')).toBeNull();
    const newest = panel.querySelector('[data-message-id="history-398"]');
    expect(newest).not.toBeNull();
    expect(mountedRows()).toBeLessThan(45);

    appendSessionMessage(sessionId, row('new-user', 'user'));
    appendSessionMessage(sessionId, { ...row('live', 'assistant'), streaming: true });
    render();
    expect(panel.querySelector('[data-message-id="history-398"]')).toBe(newest);
    const live = panel.querySelector<HTMLElement>('[data-message-id="live"]');
    expect(live?.dataset.streaming).toBe('true');
    expect(live?.dataset.chatHistoryRow).toBeUndefined();

    const oldest = revealConversationMessage(containerId, sessionId, 'history-0')!;
    expect(oldest.dataset.chatHistoryRow).toBe('user');
    const text = oldest.querySelector('.msg__text')!;
    const range = document.createRange();
    range.selectNodeContents(text);
    const selection = document.getSelection()!;
    selection.addRange(range);
    oldest.tabIndex = 0;
    oldest.focus();

    expect(revealConversationMessage(containerId, sessionId, 'history-200')?.dataset.messageId).toBe('history-200');
    expect(panel.querySelector('[data-message-id="history-0"]')).toBe(oldest);
    expect(selection.getRangeAt(0).startContainer).toBe(text);
    expect(document.activeElement).toBe(oldest);
    expect(panel.querySelector('[data-message-id="live"]')).toBe(live);
    expect(mountedRows()).toBeLessThan(45);

    oldest.blur();
    selection.removeAllRanges();
    render();
    expect(panel.querySelector('[data-message-id="history-0"]')).toBeNull();
    expect(revealConversationMessage(containerId, 'different-session', 'history-200')).toBeNull();
  });

  it('remounts an evicted thinking fold in the last user-selected state', () => {
    Object.defineProperty(panel, 'clientHeight', { configurable: true, value: 480 });
    const history = longHistory(220);
    history[1] = { ...history[1], thinking: 'Read this older analysis.' };
    replaceSessionMessages(sessionId, history);
    render();
    simulateViewportGeometry();

    const row = revealConversationMessage(containerId, sessionId, 'history-1')!;
    const detail = row.querySelector<HTMLDetailsElement>('details.process-timeline__details:not([data-fold-key])')!;
    expect(detail.open).toBe(false);
    detail.open = true;
    detail.dataset.userFoldIntent = '1';
    detail.dispatchEvent(new Event('toggle'));
    revealConversationMessage(containerId, sessionId, 'history-180');
    expect(row.isConnected).toBe(false);
    const revealed = revealConversationMessage(containerId, sessionId, 'history-1')!;
    expect(revealed).not.toBe(row);
    expect(revealed.querySelector<HTMLDetailsElement>('details.process-timeline__details:not([data-fold-key])')?.open).toBe(true);
  });

  it('pins a growing live row during resize delivery while preserving a reader who has scrolled up', () => {
    let receiveResize: ResizeObserverCallback | undefined;
    class TestResizeObserver {
      constructor(private readonly receive: ResizeObserverCallback) {}
      observe(target: Element): void {
        if (target.classList.contains('messages__inner')) receiveResize = this.receive;
      }
      unobserve(): void {}
      disconnect(): void {}
    }
    vi.stubGlobal('ResizeObserver', TestResizeObserver);
    try {
      let height = 9_000;
      let top = 0;
      Object.defineProperties(panel, {
        clientHeight: { configurable: true, value: 480 },
        scrollHeight: { configurable: true, get: () => height },
        scrollTop: {
          configurable: true,
          get: () => top,
          set: (value: number) => { top = Math.max(0, Math.min(value, height - panel.clientHeight)); },
        },
      });
      replaceSessionMessages(sessionId, [
        ...longHistory(240),
        { id: 'live-answer', role: 'assistant', content: 'Live reply', timestamp: 241, streaming: true },
      ]);
      render();
      getConversationScrollAnchor(containerId).jumpToBottom();
      const live = panel.querySelector('[data-streaming="true"]')?.closest('[data-conversation-unit]');
      expect(live).toBeTruthy();
      expect(receiveResize).toBeTypeOf('function');
      const grow = (blockSize: number) => {
        height += 44;
        receiveResize!([{ target: live, borderBoxSize: [{ blockSize, inlineSize: 800 }] } as ResizeObserverEntry],
          {} as ResizeObserver);
      };

      grow(220);
      expect(panel.scrollTop).toBe(height - panel.clientHeight);

      panel.scrollTop -= 100;
      const readingTop = panel.scrollTop;
      grow(264);
      expect(panel.scrollTop).toBe(readingTop);
      expect(getConversationScrollAnchor(containerId).isStickyBottom()).toBe(false);
    } finally {
      disposeConversationRenderer(containerId);
      vi.unstubAllGlobals();
    }
  });

  it('does not replay a pre-jump geometry anchor or discard the next correction on its scroll acknowledgment', () => {
    const paint = new Map<number, FrameRequestCallback>();
    let nextFrame = 0;
    const observers: TestResizeObserver[] = [];
    class TestResizeObserver {
      readonly watched = new Set<Element>();
      constructor(private readonly receive: ResizeObserverCallback) { observers.push(this); }
      observe(target: Element): void { this.watched.add(target); }
      unobserve(target: Element): void { this.watched.delete(target); }
      disconnect(): void { this.watched.clear(); }
      measure(target: Element, blockSize: number): void {
        this.receive([{ target, borderBoxSize: [{ blockSize, inlineSize: 800 }] } as ResizeObserverEntry],
          this as unknown as ResizeObserver);
      }
    }
    function flushFrame(): void {
      const work = Array.from(paint.entries());
      for (const [id, callback] of work) {
        if (paint.delete(id)) callback(0);
      }
    }
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
      paint.set(++nextFrame, callback);
      return nextFrame;
    });
    vi.stubGlobal('cancelAnimationFrame', (id: number) => { paint.delete(id); });
    vi.stubGlobal('ResizeObserver', TestResizeObserver);
    try {
      Object.defineProperty(panel, 'clientHeight', { configurable: true, value: 480 });
      replaceSessionMessages(sessionId, longHistory(240));
      render();
      simulateViewportGeometry();
      flushFrame();

      revealConversationMessage(containerId, sessionId, 'history-50');
      const wrapper = panel.querySelector('.messages__inner')!;
      const observer = observers.find((candidate) => candidate.watched.has(wrapper))!;
      observer.measure(panel.querySelector('[data-message-id="history-44"]')!, 222);

      revealConversationMessage(containerId, sessionId, 'history-150');
      const revealedTop = panel.scrollTop;
      flushFrame();
      expect(panel.scrollTop).toBe(revealedTop);

      observer.measure(panel.querySelector('[data-message-id="history-144"]')!, 222);
      // The delayed native event for the viewport's own scroll write can arrive
      // after a resize callback has queued an anchor correction.
      panel.dispatchEvent(new Event('scroll'));
      flushFrame();
      expect(panel.scrollTop).toBe(revealedTop + 150);

      // A genuinely new user position supersedes a queued geometry correction.
      observer.measure(panel.querySelector('[data-message-id="history-144"]')!, 300);
      panel.scrollTop -= 55;
      const userTop = panel.scrollTop;
      panel.dispatchEvent(new Event('scroll'));
      flushFrame();
      expect(panel.scrollTop).toBe(userTop);
    } finally {
      disposeConversationRenderer(containerId);
      vi.unstubAllGlobals();
    }
  });

  it('anchors the reading row across repeated prepends and restores it on a session switch', () => {
    Object.defineProperty(panel, 'clientHeight', { configurable: true, value: 480 });
    let history = longHistory(240);
    replaceSessionMessages(sessionId, history);
    render();
    simulateViewportGeometry();
    const reading = revealConversationMessage(containerId, sessionId, 'history-50');
    expect(reading).not.toBeNull();
    const initialTop = panel.scrollTop;

    for (let page = 1; page <= 5; page += 1) {
      expect(preserveConversationHistoryViewport(containerId, sessionId)).toBe(true);
      const older = Array.from({ length: 40 }, (_, index) => row(
        'older-' + page + '-' + index, index % 2 === 0 ? 'user' : 'assistant',
      ));
      history = [...older, ...history];
      replaceSessionMessages(sessionId, history);
      render();
      expect(panel.scrollTop).toBe(initialTop + page * 20 * (72 + 132));
      expect(panel.querySelector('[data-message-id="history-50"]')).toBe(reading);
      expect(mountedRows()).toBeLessThan(45);
    }

    const readingTop = panel.scrollTop;
    replaceSessionMessages('other-session', [row('other-user', 'user')]);
    renderConversation(panel, containerId, 'other-session');
    expect(panel.querySelector('[data-message-id="history-50"]')).toBeNull();
    render();
    simulateViewportGeometry();
    expect(restoreConversationViewport(containerId, sessionId)).toBe(true);
    expect(panel.scrollTop).toBe(readingTop);
    expect(panel.querySelector('[data-message-id="history-50"]')).not.toBeNull();
  });
});

describe('P0-1 渲染隔离与历史失败可见化', () => {
  beforeEach(() => {
    __resetAllStoresForTest();
    vi.mocked(reportRendererError).mockClear();
    document.body.innerHTML = '<div id="panel-a"></div>';
  });

  afterEach(() => {
    disposeConversationRenderer('panel-a');
  });

  it('单条消息构建抛错只降级为错误占位卡，其余消息照常渲染', () => {
    const panel = document.getElementById('panel-a')!;
    appendSessionMessage('sid-p0', { id: 'poison-user', role: 'user', content: '毒消息', timestamp: 1 });
    appendSessionMessage('sid-p0', { id: 'good-user', role: 'user', content: '正常消息', timestamp: 2 });

    renderConversation(panel, 'panel-a', 'sid-p0');

    // 好消息正常渲染；坏消息降级为错误占位卡；异常被上报而非逃逸。
    expect(panel.textContent).toContain('正常消息');
    expect(panel.querySelector('.render-error-card')).not.toBeNull();
    expect(reportRendererError).toHaveBeenCalledWith('render', expect.any(Error), expect.objectContaining({ unitKey: 'poison-user' }));

    // 同帧数据不变（sig 未变 → reuse）：第二次渲染不重试 build、不重复上报。
    const errorCards = () => panel.querySelectorAll('.render-error-card').length;
    const before = errorCards();
    renderConversation(panel, 'panel-a', 'sid-p0');
    expect(errorCards()).toBe(before);
  });

  it('sig 阶段抛错只降级该单元，数据变化后仍会重试', () => {
    const panel = document.getElementById('panel-a')!;
    // attachments 非数组：sigUserMessage 的 .map 抛 TypeError，且该单元 build 也被毒化。
    const badMessage = {
      id: 'poison-sig',
      role: 'user',
      content: '坏 sig 消息',
      timestamp: 1,
      attachments: 'not-an-array',
    } as unknown as ChatMessage;
    appendSessionMessage('sid-sig', badMessage);
    appendSessionMessage('sid-sig', { id: 'good-sig', role: 'user', content: '正常消息', timestamp: 2 });

    // 整轮渲染不抛错：坏单元降级为错误占位卡，同屏正常消息照常渲染。
    expect(() => renderConversation(panel, 'panel-a', 'sid-sig')).not.toThrow();
    expect(panel.textContent).toContain('正常消息');
    expect(panel.querySelectorAll('.render-error-card')).toHaveLength(1);
    expect(reportRendererError).toHaveBeenCalledWith(
      'render',
      expect.any(Error),
      expect.objectContaining({ unitKey: 'poison-sig', phase: 'sig' }),
    );

    // 数据变化 → 兜底摘要变化 → sig 变化 → 重新尝试 build（再次上报），不会永久停在错误卡。
    const reportsBefore = vi.mocked(reportRendererError).mock.calls.length;
    replaceSessionMessages('sid-sig', [
      { ...badMessage, content: '坏 sig 消息（已更新）' },
      { id: 'good-sig', role: 'user', content: '正常消息', timestamp: 2 },
    ] as ChatMessage[]);
    renderConversation(panel, 'panel-a', 'sid-sig');
    expect(vi.mocked(reportRendererError).mock.calls.length).toBeGreaterThan(reportsBefore);
  });

  it('历史加载失败且无消息时渲染「加载失败 + 重试」卡，清除标记后恢复默认空态', () => {
    const panel = document.getElementById('panel-a')!;
    markHistoryLoadError('sid-err');

    renderConversation(panel, 'panel-a', 'sid-err');
    expect(panel.textContent).toContain('会话历史加载失败');
    expect(panel.querySelector('[data-history-retry]')).not.toBeNull();

    clearHistoryLoadError('sid-err');
    renderConversation(panel, 'panel-a', 'sid-err');
    expect(panel.textContent).not.toContain('会话历史加载失败');
  });

  it('首屏历史窗口在途时渲染加载骨架（P1-6），结束后回落空态', () => {
    const panel = document.getElementById('panel-a')!;
    setHistoryLoading('sid-loading', true);

    renderConversation(panel, 'panel-a', 'sid-loading');
    expect(panel.textContent).toContain('正在加载会话历史…');
    expect(panel.querySelector('[data-history-retry]')).toBeNull();

    setHistoryLoading('sid-loading', false);
    renderConversation(panel, 'panel-a', 'sid-loading');
    expect(panel.textContent).not.toContain('正在加载会话历史…');
  });
});

describe('P0-1 渲染合并器异常守卫', () => {
  it('render 抛错被上报且不阻断后续帧调度', () => {
    const queue: Array<() => void> = [];
    const scheduler = (cb: () => void) => queue.push(cb);
    let calls = 0;
    const schedule = createChatRenderCoalescer(() => {
      calls += 1;
      if (calls === 1) throw new Error('render boom');
    }, scheduler);

    schedule();
    expect(() => queue.splice(0).forEach((fn) => fn())).not.toThrow();
    expect(reportRendererError).toHaveBeenCalledWith('render', expect.any(Error));

    // 下一帧照常可调度、可执行（循环未卡死）。
    schedule();
    queue.splice(0).forEach((fn) => fn());
    expect(calls).toBe(2);
  });

  it('streaming patch 抛错被上报', () => {
    const queue: Array<() => void> = [];
    const scheduler = (cb: () => void) => queue.push(cb);
    const coalescer = createStreamingPatchCoalescer(() => {
      throw new Error('patch boom');
    }, scheduler);

    coalescer.schedule({ sid: 's1', assistantId: 'a1' });
    expect(() => queue.splice(0).forEach((fn) => fn())).not.toThrow();
    expect(reportRendererError).toHaveBeenCalledWith('streaming-patch', expect.any(Error));
  });
});
