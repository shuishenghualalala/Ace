import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown, { defaultUrlTransform } from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";
import "highlight.js/styles/github.css";
import "../styles/markdown.css";
import { preprocessStreamMarkdown } from "../lib/markdown-stream";

interface Props {
  content: string;
  /** 是否处于流式输出中。为 true 时会先自动闭合未完成的 Markdown 语法，避免 `**bold` 半截闪烁。 */
  isStreaming?: boolean;
  /**
   * 是否对超长内容进行增量渲染。开启后，Markdown 会按段落块分批解析，
   * 通过 IntersectionObserver 在滚动接近底部时自动加载下一块，
   * 避免 react-markdown + remarkGfm + rehypeHighlight 一次性解析长文导致页面卡死。
   */
  fold?: boolean;
  /** 首屏渲染的字符阈值，默认 4000。 */
  initialThreshold?: number;
  /** 每次追加渲染的字符阈值，默认 2000。 */
  stepThreshold?: number;
  /**
   * 传入后启用 Wiki 双链：正文中的 [[页面标题]] 渲染为可点击链接，
   * 点击时回调标题（代码围栏内的 [[...]] 不转换）。对齐桌面端详情页行为。
   */
  onWikiLink?: (title: string) => void;
}

const DEFAULT_INITIAL_THRESHOLD = 4000;
const DEFAULT_STEP_THRESHOLD = 2000;

const WIKILINK_SCHEME = "wikilink://";

/** 代码围栏状态机，供 linkifyWikiLinks 和 splitBlocks 共用。 */
class FenceTracker {
  private state: { marker: string; indent: string } | null = null;

  get inside(): boolean {
    return this.state !== null;
  }

  /** 处理一行文本，根据围栏状态决定进入/退出。 */
  process(line: string): void {
    const match = line.match(FENCE_RE);
    if (this.state) {
      if (match && this.isCloseMatch(match)) {
        this.state = null;
      }
    } else if (match) {
      this.state = { marker: match[2], indent: match[1] };
    }
  }

  private isCloseMatch(
    match: RegExpMatchArray,
  ): boolean {
    const state = this.state!;
    return (
      match[1] === state.indent &&
      (match[3] ?? "").trim() === "" &&
      match[2].startsWith(state.marker[0]) &&
      match[2].length >= state.marker.length
    );
  }
}

const FENCE_RE = /^([ \t]*)(`{3,}|~{3,})([^\n]*)$/;

/**
 * 把正文中的 [[页面标题]] 转成 markdown 链接 [标题](wikilink://标题)，
 * 交由 a 渲染器统一处理为可点击的双链。代码围栏内的内容原样保留。
 */
function linkifyWikiLinks(text: string): string {
  const fence = new FenceTracker();
  return text
    .split("\n")
    .map((line) => {
      if (fence.inside) {
        fence.process(line);
        return line;
      }
      fence.process(line);
      if (fence.inside) return line; // 本行是围栏起始行
      return line.replace(/\[\[([^\]]+)\]\]/g, (_m, title: string) => {
        const trimmed = title.trim();
        if (!trimmed) return _m;
        return `[${trimmed}](${WIKILINK_SCHEME}${encodeURIComponent(trimmed)})`;
      });
    })
    .join("\n");
}

/** 把 markdown 拆成相互独立的渲染块。代码围栏与 GFM 表格必须整体出现，否则 fold 会切断结构。 */
function splitBlocks(text: string): string[] {
  const blocks: string[] = [];
  const current: string[] = [];
  const fence = new FenceTracker();

  const flush = (): void => {
    if (current.length === 0) return;
    const joined = current.join("\n");
    if (joined.trim().length > 0) blocks.push(joined);
    current.length = 0;
  };

  for (const line of text.split("\n")) {
    const trimmed = line.trim();

    if (fence.inside) {
      current.push(line);
      fence.process(line);
      if (!fence.inside) flush(); // 围栏关闭
      continue;
    }

    fence.process(line);
    if (fence.inside) {
      flush();
      current.push(line);
      continue;
    }

    // GFM 表格：连续以 | 开头的行，遇到空行结束。
    if (trimmed.startsWith("|")) {
      if (current.length > 0 && !current[0].trim().startsWith("|")) flush();
      current.push(line);
      continue;
    }

    if (current.length > 0 && current[0].trim().startsWith("|")) {
      if (trimmed === "") {
        flush();
      } else {
        current.push(line);
      }
      continue;
    }

    // 普通段落按空行分块。
    if (trimmed === "") {
      flush();
      continue;
    }

    current.push(line);
  }

  flush();
  return blocks;
}

/** 按字符阈值取前若干个完整段落块。 */
function takeBlocksUntil(blocks: string[], threshold: number): number {
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

export default function MarkdownContent({
  content,
  isStreaming,
  fold = false,
  initialThreshold = DEFAULT_INITIAL_THRESHOLD,
  stepThreshold = DEFAULT_STEP_THRESHOLD,
  onWikiLink,
}: Props) {
  const safeContent = isStreaming ? preprocessStreamMarkdown(content, true) : content;
  // SSR / 首次渲染时通过计算值确定可见块数；hydrate 后由 effect 同步，避免空白。
  const [visibleCount, setVisibleCount] = useState<number | null>(null);
  const sentinelRef = useRef<HTMLDivElement | null>(null);
  const loadingRef = useRef(false);
  const timerRef = useRef<number | null>(null);

  const blocks = useMemo(() => splitBlocks(safeContent), [safeContent]);

  const targetVisibleCount = fold && !isStreaming ? takeBlocksUntil(blocks, initialThreshold) : blocks.length;
  const computedVisibleCount = visibleCount ?? targetVisibleCount;

  useEffect(() => {
    setVisibleCount(targetVisibleCount);
    loadingRef.current = false;
    if (timerRef.current) {
      window.clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }, [targetVisibleCount]);

  useEffect(() => {
    return () => {
      if (timerRef.current) {
        window.clearTimeout(timerRef.current);
      }
    };
  }, []);

  const loadMore = useCallback(() => {
    if (loadingRef.current) return;
    loadingRef.current = true;
    if (timerRef.current) window.clearTimeout(timerRef.current);
    timerRef.current = window.setTimeout(() => {
      setVisibleCount((prev) => {
        const current = prev ?? targetVisibleCount;
        const nextLengthTarget = current > 0 ? current + stepThreshold : initialThreshold;
        const nextCount = takeBlocksUntil(blocks, nextLengthTarget);
        return Math.max(current, nextCount);
      });
      loadingRef.current = false;
    }, 150);
  }, [blocks, initialThreshold, stepThreshold, targetVisibleCount]);

  useEffect(() => {
    if (!fold || computedVisibleCount >= blocks.length) return;

    const sentinel = sentinelRef.current;
    if (!sentinel) return;

    if (typeof window === "undefined" || !("IntersectionObserver" in window)) {
      return;
    }

    const observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting) {
            loadMore();
          }
        });
      },
      { root: null, rootMargin: "200px", threshold: 0 }
    );

    observer.observe(sentinel);
    return () => observer.disconnect();
  }, [fold, blocks.length, computedVisibleCount, loadMore]);

  const visibleBlocks = blocks.slice(0, computedVisibleCount);
  const hasMore = fold && computedVisibleCount < blocks.length;

  // 把可见块拼成一份 markdown 交给单个 ReactMarkdown 实例解析，避免每个段落块都重建 remark/rehype 插件。
  const markdownSource = useMemo(() => {
    const joined = visibleBlocks.join("\n\n");
    return onWikiLink ? linkifyWikiLinks(joined) : joined;
  }, [visibleBlocks, onWikiLink]);

  const components = useMemo(
    () => ({
      pre: ({ children }: { children?: React.ReactNode }) => <pre className="md-pre">{children}</pre>,
      code: ({ className, children, ...rest }: any) => {
        const isInline = !className;
        return isInline ? (
          <code className="md-inline-code" {...rest}>
            {children}
          </code>
        ) : (
          <code className={className} {...rest}>
            {children}
          </code>
        );
      },
      table: ({ children }: { children?: React.ReactNode }) => (
        <div className="md-table-wrap">
          <table>{children}</table>
        </div>
      ),
      a: ({ href, children }: { href?: string; children?: React.ReactNode }) => {
        const target = String(href || "");
        if (target.startsWith("mention://")) {
          const [, rest = ""] = target.split("mention://", 2);
          const [kind = "member", rawId = ""] = rest.split("/", 2);
          const label = String(children || "").trim() || `@${decodeURIComponent(rawId)}`;
          return (
            <span className={`md-mention md-mention--${kind}`} data-mention-id={rawId}>
              {label}
            </span>
          );
        }
        if (target.startsWith(WIKILINK_SCHEME) && onWikiLink) {
          const title = decodeURIComponent(target.slice(WIKILINK_SCHEME.length));
          return (
            <a
              className="md-wikilink"
              href={target}
              title={`打开 Wiki 页面：${title}`}
              onClick={(e) => {
                e.preventDefault();
                onWikiLink(title);
              }}
            >
              {children}
            </a>
          );
        }
        return (
          <a href={href} target="_blank" rel="noopener noreferrer">
            {children}
          </a>
        );
      },
    }),
    [onWikiLink]
  );

  return (
    <>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeHighlight]}
        urlTransform={(url) => (url.startsWith("mention://") || url.startsWith(WIKILINK_SCHEME)) ? url : defaultUrlTransform(url)}
        components={components}
      >
        {markdownSource}
      </ReactMarkdown>
      {hasMore && (
        <div ref={sentinelRef} className="md-sentinel" aria-hidden="true">
          {typeof window !== "undefined" && "IntersectionObserver" in window ? null : (
            <button className="md-fold-toggle" onClick={() => loadMore()} type="button">
              继续加载
            </button>
          )}
        </div>
      )}
    </>
  );
}
