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
import { formatMessageTime, formatDuration, renderQueuePanelHtml, sessionStatusClass } from '../../src/ui/chat-render';
import {
  disposeConversationRenderer,
  getConversationScrollAnchor,
  renderConversation,
} from '../../src/ui/features/conversation-renderer';
import { __resetAllStoresForTest } from '../../src/ui/stores/stores';
import { appendSessionMessage } from '../../src/ui/state';

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
    appendSessionMessage('sid-a', { id: 'u-a2', role: 'user', content: '面板A第二条', timestamp: 2 });
    renderConversation(panelA, 'panel-a', 'sid-a');
    expect(panelA.textContent).toContain('面板A第二条');
    expect(panelB.querySelector('.msg')).toBe(bFirst);

    // A 切到 B 的会话：只重置 A 容器的缓存；B 的 DOM 仍保持原节点
    renderConversation(panelA, 'panel-a', 'sid-b');
    expect(panelA.textContent).toContain('面板B消息');
    expect(panelA.textContent).not.toContain('面板A第二条');
    expect(panelB.querySelector('.msg')).toBe(bFirst);
  });
});
