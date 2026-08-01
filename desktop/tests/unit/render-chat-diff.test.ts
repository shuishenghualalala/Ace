/**
 * 流式渲染纯逻辑单测。
 * 覆盖 applyFoldState（toggle 事件委托）+ createChatRenderCoalescer（渲染合并）。
 *
 * X3a（2026-06）：补充 chat-render 导出的纯逻辑 helper 单测
 * （formatMessageTime / formatDuration / sessionStatusClass）。
 * 注：render* 现在返回 DOM 节点（HTMLElement），而 vitest environment 为 node（无 DOM），
 * 故 DOM-shape 断言无法在此环境运行；它们的等价性由「typecheck + 行为不变的 renderChat」
 * 共同保证。DOM-shape 测试待切到 happy-dom/jsdom 环境后再补。
 */
import { describe, it, expect } from 'vitest';
import {
  applyFoldState,
  createChatRenderCoalescer,
  createStreamingPatchCoalescer,
  type FoldSets,
} from '../../src/ui/render-utils';
import { formatMessageTime, formatDuration, renderQueuePanelHtml, sessionStatusClass } from '../../src/ui/chat-render';

describe('applyFoldState', () => {
  function makeSets(): FoldSets {
    return { unfolded: new Set<string>(), folded: new Set<string>() };
  }

  it('open=true → add to unfolded, remove from folded', () => {
    const sets = makeSets();
    sets.folded.add('t1');
    applyFoldState('t1', true, sets);
    expect(sets.unfolded.has('t1')).toBe(true);
    expect(sets.folded.has('t1')).toBe(false);
  });

  it('open=false → add to folded, remove from unfolded', () => {
    const sets = makeSets();
    sets.unfolded.add('t2');
    applyFoldState('t2', false, sets);
    expect(sets.folded.has('t2')).toBe(true);
    expect(sets.unfolded.has('t2')).toBe(false);
  });

  it('toggling same turn moves it between sets', () => {
    const sets = makeSets();
    applyFoldState('t3', false, sets);
    expect(Array.from(sets.folded)).toEqual(['t3']);
    applyFoldState('t3', true, sets);
    expect(Array.from(sets.folded)).toEqual([]);
    expect(Array.from(sets.unfolded)).toEqual(['t3']);
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
