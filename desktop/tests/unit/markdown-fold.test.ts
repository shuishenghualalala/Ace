/**
 * @vitest-environment happy-dom
 *
 * markdown-fold 单测：长 Markdown 增量渲染（对齐 web MarkdownContent 的 fold 模式）。
 * 覆盖：结构块拆分（段落/代码围栏/GFM 表格）、阈值取块、
 *       短文档全量渲染、长文档首屏 + IntersectionObserver 分批追加、
 *       无 IntersectionObserver 环境回退全量渲染、dispose 清理。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  mountFoldedMarkdown,
  splitMarkdownBlocks,
  takeBlocksUntil,
} from '../../src/ui/markdown-fold';

class FakeIntersectionObserver {
  static instances: FakeIntersectionObserver[] = [];
  callback: IntersectionObserverCallback;
  disconnected = false;

  constructor(callback: IntersectionObserverCallback) {
    this.callback = callback;
    FakeIntersectionObserver.instances.push(this);
  }

  observe(): void {}
  unobserve(): void {}
  disconnect(): void {
    this.disconnected = true;
  }

  trigger(): void {
    this.callback(
      [{ isIntersecting: true } as IntersectionObserverEntry],
      this as unknown as IntersectionObserver,
    );
  }
}

function stubIntersectionObserver(): void {
  FakeIntersectionObserver.instances = [];
  (globalThis as Record<string, unknown>).IntersectionObserver = FakeIntersectionObserver;
}

function restoreIntersectionObserver(): void {
  delete (globalThis as Record<string, unknown>).IntersectionObserver;
}

/** 生成 count 个段落块，每块约 blockSize 字符。 */
function makeLongMarkdown(count: number, blockSize = 50): string {
  return Array.from({ length: count }, (_, i) => `段落${i} ${'x'.repeat(blockSize)}`).join('\n\n');
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  restoreIntersectionObserver();
  document.body.innerHTML = '';
});

describe('splitMarkdownBlocks', () => {
  it('普通段落按空行分块', () => {
    expect(splitMarkdownBlocks('第一段\n\n第二段\n\n\n第三段')).toEqual(['第一段', '第二段', '第三段']);
  });

  it('代码围栏整体成块（含语言标注）', () => {
    const source = '前文\n\n```js\nconsole.log(1)\n\n空行不断开\n```\n\n后文';
    expect(splitMarkdownBlocks(source)).toEqual(['前文', '```js\nconsole.log(1)\n\n空行不断开\n```', '后文']);
  });

  it('~~~ 围栏同样整体成块', () => {
    const source = '~~~\ncode\n~~~\n\n后文';
    expect(splitMarkdownBlocks(source)).toEqual(['~~~\ncode\n~~~', '后文']);
  });

  it('GFM 表格整体成块', () => {
    const source = '| a | b |\n|--|--|\n| 1 | 2 |\n\n后文';
    expect(splitMarkdownBlocks(source)).toEqual(['| a | b |\n|--|--|\n| 1 | 2 |', '后文']);
  });

  it('纯空白输入不产生块', () => {
    expect(splitMarkdownBlocks('\n\n  \n')).toEqual([]);
  });
});

describe('takeBlocksUntil', () => {
  it('阈值内取完整块，块间计 2 字符拼接成本', () => {
    const blocks = ['aaaa', 'bbbb', 'cccc'];
    // 4 → 1 块；4+2+4=10 → 2 块；再加 4+2+4=16
    expect(takeBlocksUntil(blocks, 4)).toBe(1);
    expect(takeBlocksUntil(blocks, 10)).toBe(2);
    expect(takeBlocksUntil(blocks, 100)).toBe(3);
  });

  it('空数组返回 0', () => {
    expect(takeBlocksUntil([], 100)).toBe(0);
  });
});

describe('mountFoldedMarkdown', () => {
  it('短文档一次性全量渲染，无哨兵', () => {
    stubIntersectionObserver();
    const container = document.createElement('div');
    mountFoldedMarkdown(container, '**粗体** 正文');
    expect(container.innerHTML).toContain('<strong>粗体</strong>');
    expect(container.querySelector('.md-sentinel')).toBeNull();
    expect(FakeIntersectionObserver.instances).toHaveLength(0);
  });

  it('无 IntersectionObserver 环境回退全量渲染', () => {
    restoreIntersectionObserver();
    const container = document.createElement('div');
    mountFoldedMarkdown(container, makeLongMarkdown(10), { initialThreshold: 120 });
    expect(container.textContent).toContain('段落0');
    expect(container.textContent).toContain('段落9');
    expect(container.querySelector('.md-sentinel')).toBeNull();
  });

  it('长文档首屏只渲染前一批，滚动触发后追加，渲完移除哨兵并断开 observer', () => {
    stubIntersectionObserver();
    document.body.innerHTML = '<div id="host"></div>';
    const container = document.querySelector<HTMLElement>('#host')!;
    mountFoldedMarkdown(container, makeLongMarkdown(10), {
      initialThreshold: 120,
      stepThreshold: 120,
    });

    // 首屏：只有前两块，哨兵存在
    expect(container.textContent).toContain('段落0');
    expect(container.textContent).toContain('段落1');
    expect(container.textContent).not.toContain('段落9');
    const sentinel = container.querySelector('.md-sentinel');
    expect(sentinel).not.toBeNull();
    expect(FakeIntersectionObserver.instances).toHaveLength(1);
    const observer = FakeIntersectionObserver.instances[0];

    // 连续触发直到全部渲染完
    for (let i = 0; i < 10 && container.querySelector('.md-sentinel'); i++) {
      observer.trigger();
      vi.advanceTimersByTime(200);
    }
    expect(container.textContent).toContain('段落9');
    expect(container.querySelector('.md-sentinel')).toBeNull();
    expect(observer.disconnected).toBe(true);
  });

  it('触发去抖：150ms 内重复触发只追加一批', () => {
    stubIntersectionObserver();
    document.body.innerHTML = '<div id="host"></div>';
    const container = document.querySelector<HTMLElement>('#host')!;
    mountFoldedMarkdown(container, makeLongMarkdown(10), {
      initialThreshold: 60,
      stepThreshold: 60,
    });
    const observer = FakeIntersectionObserver.instances[0];

    observer.trigger();
    observer.trigger();
    observer.trigger();
    vi.advanceTimersByTime(200);

    // 去抖后只前进一批（首屏 1 块 + 1 块）
    expect(container.textContent).toContain('段落1');
    expect(container.textContent).not.toContain('段落2');
  });

  it('dispose 断开 observer 并清理计时器', () => {
    stubIntersectionObserver();
    document.body.innerHTML = '<div id="host"></div>';
    const container = document.querySelector<HTMLElement>('#host')!;
    const handle = mountFoldedMarkdown(container, makeLongMarkdown(10), {
      initialThreshold: 60,
      stepThreshold: 60,
    });
    const observer = FakeIntersectionObserver.instances[0];

    observer.trigger();
    handle.dispose();
    vi.advanceTimersByTime(500);

    expect(observer.disconnected).toBe(true);
    // 计时器已清，不再追加
    expect(container.textContent).not.toContain('段落2');
  });
});
