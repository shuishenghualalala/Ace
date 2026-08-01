/**
 * 长 Markdown 增量渲染（对齐 web 端 MarkdownContent 的 fold 模式）。
 *
 * 整篇 micromark + KaTeX + DOMPurify 同步解析是长文档卡死的根源，
 * 这里把文档按结构块拆分：首屏只渲染前 initialThreshold 字符，
 * 滚动接近底部哨兵（IntersectionObserver）时再渲染下一批。
 * 无 IntersectionObserver 的环境（如 happy-dom 单测）回退为一次性全量渲染。
 */

import { renderMarkdownHtml } from './markdown';

const DEFAULT_INITIAL_THRESHOLD = 4000;
const DEFAULT_STEP_THRESHOLD = 2000;
const LOAD_MORE_DEBOUNCE_MS = 150;

/** 把 markdown 拆成相互独立的渲染块。代码围栏与 GFM 表格必须整体出现，否则增量渲染会切断结构。 */
export function splitMarkdownBlocks(text: string): string[] {
  const lines = text.split('\n');
  const blocks: string[] = [];
  const current: string[] = [];
  let inFence: { marker: string; indent: string } | null = null;

  const flush = (): void => {
    if (current.length === 0) return;
    const joined = current.join('\n');
    if (joined.trim().length > 0) blocks.push(joined);
    current.length = 0;
  };

  for (const line of lines) {
    const trimmed = line.trim();

    if (inFence) {
      current.push(line);
      const fenceMatch = line.match(/^([ \t]*)(`{3,}|~{3,})([^\n]*)$/);
      if (
        fenceMatch &&
        fenceMatch[1] === inFence.indent &&
        (fenceMatch[3] ?? '').trim() === '' &&
        fenceMatch[2].startsWith(inFence.marker[0]) &&
        fenceMatch[2].length >= inFence.marker.length
      ) {
        flush();
        inFence = null;
      }
      continue;
    }

    const fenceMatch = line.match(/^([ \t]*)(`{3,}|~{3,})([^\n]*)$/);
    if (fenceMatch) {
      flush();
      inFence = { marker: fenceMatch[2], indent: fenceMatch[1] };
      current.push(line);
      continue;
    }

    // GFM 表格：连续以 | 开头的行，遇到空行结束。
    if (trimmed.startsWith('|')) {
      if (current.length > 0 && !current[0].trim().startsWith('|')) flush();
      current.push(line);
      continue;
    }

    if (current.length > 0 && current[0].trim().startsWith('|')) {
      if (trimmed === '') {
        flush();
      } else {
        current.push(line);
      }
      continue;
    }

    // 普通段落按空行分块。
    if (trimmed === '') {
      flush();
      continue;
    }

    current.push(line);
  }

  flush();
  return blocks;
}

/** 按字符阈值取前若干个完整段落块。 */
export function takeBlocksUntil(blocks: string[], threshold: number): number {
  let length = 0;
  for (let i = 0; i < blocks.length; i++) {
    const addLen = i > 0 ? blocks[i].length + 2 : blocks[i].length;
    if (i > 0 && length + addLen > threshold) {
      return i;
    }
    length += addLen;
  }
  return blocks.length;
}

export interface FoldedMarkdownHandle {
  /** 断开 IntersectionObserver 并清理去抖计时器（容器被销毁前调用）。 */
  dispose(): void;
}

export interface MountFoldedMarkdownOptions {
  /** 首屏渲染的字符阈值，默认 4000。 */
  initialThreshold?: number;
  /** 每次追加渲染的字符阈值，默认 2000。 */
  stepThreshold?: number;
}

/**
 * 把 Markdown 增量渲染进 container（调用前 container 应为空）。
 * 短文档一次性渲染；长文档先渲染首屏块，滚动接近底部时按批追加。
 */
export function mountFoldedMarkdown(
  container: HTMLElement,
  content: string,
  options: MountFoldedMarkdownOptions = {},
): FoldedMarkdownHandle {
  const initialThreshold = options.initialThreshold ?? DEFAULT_INITIAL_THRESHOLD;
  const stepThreshold = options.stepThreshold ?? DEFAULT_STEP_THRESHOLD;
  const blocks = splitMarkdownBlocks(content);
  const initialCount = takeBlocksUntil(blocks, initialThreshold);
  const hasObserver = typeof IntersectionObserver !== 'undefined';

  // 短文档 / 无 IntersectionObserver：保持原有一次性全量渲染。
  if (initialCount >= blocks.length || !hasObserver) {
    container.innerHTML = renderMarkdownHtml(content);
    return { dispose: () => undefined };
  }

  let renderedCount = initialCount;
  container.innerHTML = renderMarkdownHtml(blocks.slice(0, renderedCount).join('\n\n'));
  const sentinel = document.createElement('div');
  sentinel.className = 'md-sentinel';
  sentinel.setAttribute('aria-hidden', 'true');
  container.appendChild(sentinel);

  let loading = false;
  let timer: number | null = null;

  const finish = (): void => {
    observer.disconnect();
    sentinel.remove();
  };

  const loadMore = (): void => {
    if (loading) return;
    loading = true;
    if (timer !== null) window.clearTimeout(timer);
    timer = window.setTimeout(() => {
      timer = null;
      loading = false;
      if (!sentinel.isConnected) return;
      const currentLength = blocks
        .slice(0, renderedCount)
        .reduce((sum, block, i) => sum + block.length + (i > 0 ? 2 : 0), 0);
      const nextCount = Math.max(
        renderedCount + 1,
        takeBlocksUntil(blocks, currentLength + stepThreshold),
      );
      const html = renderMarkdownHtml(blocks.slice(renderedCount, nextCount).join('\n\n'));
      renderedCount = nextCount;
      sentinel.insertAdjacentHTML('beforebegin', html);
      if (renderedCount >= blocks.length) finish();
    }, LOAD_MORE_DEBOUNCE_MS);
  };

  const observer = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) {
        if (entry.isIntersecting) loadMore();
      }
    },
    { root: null, rootMargin: '200px', threshold: 0 },
  );
  observer.observe(sentinel);

  return {
    dispose: () => {
      observer.disconnect();
      if (timer !== null) {
        window.clearTimeout(timer);
        timer = null;
      }
    },
  };
}
