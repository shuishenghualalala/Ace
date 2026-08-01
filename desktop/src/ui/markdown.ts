/**
 * Markdown → HTML 渲染（assistant 气泡正文专用）。
 *
 * 实现：micromark + GFM + 数学公式扩展。
 *  - 支持完整 GFM：blockquote、有序列表、嵌套列表、删除线、任务列表、autolink、表格。
 *  - 支持数学公式：inline `$E=mc^2$` 和 block `$$...$$`，由 micromark-extension-math + KaTeX 渲染。
 *    KaTeX 输出 MathML + HTML span 结构，需要引入 katex.css 才能正确排版。
 *  - 流式中通过 markdown-stream.ts 自动闭合未完成语法（含未闭合 `$`/`$$`），避免「先源码后样式」闪烁。
 *  - DOMPurify 兜底 XSS 防线（micromark 默认已 escape 原始 HTML，DOMPurify 作为第二层）。
 *  - postProcess 里只做 micromark 不输出但 UI/Electron 需要的适配：代码块语言标签头 + 复制按钮 wrapper、
 *    外部链接 target=_blank。table / hr 直接吃 CSS 的 `.chat-markdown table` 等元素选择器，不注入 class。
 */

import { micromark } from 'micromark';
import { gfm, gfmHtml } from 'micromark-extension-gfm';
import { math, mathHtml } from 'micromark-extension-math';
import DOMPurify from 'dompurify';

import { preprocessStreamMarkdown } from './markdown-stream';
import { escapeHtml } from '../shared/html';

/**
 * 是否在浏览器渲染进程内（DOMPurify 需要 window）。
 * 单测环境（happy-dom）也满足；node 纯逻辑测试会短路到不 sanitize 的分支。
 */
function hasDom(): boolean {
  return typeof window !== 'undefined' && typeof window.document !== 'undefined';
}

/** DOMPurify hook：禁止所有脚本类标签与危险属性。DOMPurify 默认已如此，这里显式再声明一次便于审阅。 */
function buildSanitizer(): typeof DOMPurify {
  // DOMPurify 在浏览器里 import 后即可用；这里返回实例便于未来扩展白名单。
  // 当前不放宽任何默认限制——micromark 的输出本身就在白名单内。
  return DOMPurify;
}

/**
 * micromark 输出后处理：只补 micromark 不会输出、但 UI/Electron 行为需要的东西。
 *
 * 1. 代码块：micromark 输出 `<pre><code class="language-X">...</code></pre>`，
 *    UI 需要带语言标签头 + 复制按钮的 wrapper（.code-block-wrapper / .code-block-header /
 *    .code-block-lang / .chat-md-code / .code-block-copy），这里包成该结构以维持既有 CSS。
 * 2. 外部 http(s) 链接：补 `target="_blank" rel="noopener noreferrer"`——
 *    在 Electron 渲染进程里点链接应走外部浏览器，而不是导航当前 renderer。
 *
 * 不做的事：table / hr 不再注入 class——CSS 用 `.chat-markdown table` 等元素选择器即可覆盖。
 */
function addCustomClasses(html: string): string {
  let out = html;
  // 外部 http(s) 链接：补 target/rel。仅 http(s)——相对链接、内部锚点不动。
  // 用属性边界匹配避免误改已经带 target 的（虽然 micromark 不会自带）。
  out = out.replace(
    /<a href="(https?:\/\/[^"]*)">/g,
    '<a href="$1" target="_blank" rel="noopener noreferrer">',
  );
  // 代码块复制按钮：放在 header 右侧，data-copy 标记供 attachCopyButtons 识别。
  const copyBtn = '<button class="code-block-copy" data-copy type="button">复制</button>';
  // mermaid 代码块：先于普通代码块匹配，包成 mermaid 占位 div（不含复制按钮/语言标签头）。
  // mermaid-render.ts 会在 DOM 挂载后懒加载 mermaid.js 把 [data-mermaid] 替换成 SVG。
  // 占位里保留 escaped 源码，mermaid.run 从 textContent 读取（DOM 自动 unescape）。
  out = out.replace(
    /<pre><code class="language-mermaid">([\s\S]*?)<\/code><\/pre>/g,
    (_m, code: string) => `<div class="mermaid" data-mermaid>${code}</div>`,
  );
  // 代码块：micromark 输出 `<pre><code class="language-X">...</code></pre>`，
  // 包成带语言标签头 + 复制按钮的 wrapper 以维持 CSS。
  out = out.replace(
    /<pre><code class="language-([a-zA-Z0-9+#-]*)">([\s\S]*?)<\/code><\/pre>/g,
    (_m, lang: string, code: string) => {
      const langLabel = lang ? `<span class="code-block-lang">${lang}</span>` : '';
      return `<div class="code-block-wrapper"><div class="code-block-header">${langLabel}${copyBtn}</div><pre class="chat-md-code"><code>${code}</code></pre></div>`;
    },
  );
  // 无语言标签的代码块：micromark 输出 `<pre><code>...</code></pre>`，同样包成 wrapper。
  out = out.replace(
    /<pre><code>([\s\S]*?)<\/code><\/pre>/g,
    (_m, code: string) =>
      `<div class="code-block-wrapper"><div class="code-block-header">${copyBtn}</div><pre class="chat-md-code"><code>${code}</code></pre></div>`,
  );
  return out;
}

function isTableSeparatorCell(cell: string): boolean {
  return /^:?-{3,}:?$/.test(cell.trim());
}

function renderTableRow(cells: string[]): string {
  return `| ${cells.map((cell) => cell.trim()).join(' | ')} |`;
}

/**
 * LLM 偶尔会把 GFM 表格压成一行：
 * `| A | B | |---|---| x | y |`。micromark 按规范会把它当普通段落。
 * 这里只修这种含 separator row 的紧凑表格，不猜普通 pipe 文本。
 */
function normalizeCompactPipeTables(source: string): string {
  return source
    .split('\n')
    .map((line) => {
      const trimmed = line.trim();
      if (!trimmed.startsWith('|') || !/\|\s*\|/.test(trimmed)) return line;

      const rawCells = trimmed.split('|').map((cell) => cell.trim());
      if (rawCells[0] === '') rawCells.shift();
      if (rawCells[rawCells.length - 1] === '') rawCells.pop();

      const separatorStart = rawCells.findIndex(isTableSeparatorCell);
      if (separatorStart <= 0) return line;

      let separatorEnd = separatorStart;
      while (separatorEnd < rawCells.length && isTableSeparatorCell(rawCells[separatorEnd] ?? '')) {
        separatorEnd += 1;
      }

      const separators = rawCells.slice(separatorStart, separatorEnd);
      const columnCount = separators.length;
      if (columnCount < 2) return line;

      const header = rawCells.slice(0, separatorStart).filter(Boolean);
      if (header.length !== columnCount) return line;

      const rest = rawCells.slice(separatorEnd).filter(Boolean);
      if (rest.length < columnCount) return line;

      const rows = [renderTableRow(header), renderTableRow(separators)];
      for (let i = 0; i + columnCount <= rest.length; i += columnCount) {
        rows.push(renderTableRow(rest.slice(i, i + columnCount)));
      }
      return rows.join('\n');
    })
    .join('\n');
}

/**
 * 把 Markdown 源文本转为可插入 DOM 的 HTML 字符串。
 *
 * @param source markdown 源文本
 * @param options.isStreaming 是否处于流式状态——true 时跑流式预处理（自动闭合未完成语法）。
 *   非流式（已结束的回合、历史回放）也安全调用，预处理对完整 markdown 是幂等的。
 */
export function renderMarkdownHtml(
  source: string,
  options: { allowImages?: boolean; isStreaming?: boolean } = {},
): string {
  if (!source || !source.trim()) return '';

  // 流式时才跑自动闭合预处理；非流式（final/历史回放）直接按原文渲染，
  // 避免对已经成型的 `**` 等标记做错误的补闭合。
  const afterStream = options.isStreaming ? preprocessStreamMarkdown(source, true) : source;
  const preprocessed = normalizeCompactPipeTables(afterStream);

  // 提取 [[Wiki 链接]]，替换为占位符，避免被 micromark 误处理
  const wikiLinks: string[] = [];
  const withPlaceholders = preprocessed.replace(/\[\[([^\]]+)\]\]/g, (_m, title: string) => {
    const idx = wikiLinks.length;
    wikiLinks.push(title);
    return `〈WIKI_LINK_${idx}〉`;
  });

  // micromark：默认 allowDangerousHtml=false → 原始 HTML 被 escape；
  // 默认 allowDangerousProtocol=false → javascript: / data: 等危险协议被丢弃。
  // 扩展：GFM（表格/删除线/任务列表/autolink）+ math（$..$ / $$..$$，KaTeX 输出 HTML）。
  const raw = micromark(withPlaceholders, {
    extensions: [gfm(), math()],
    htmlExtensions: [gfmHtml(), mathHtml()],
  });

  const withClasses = addCustomClasses(raw);

  /** 把占位符还原为可点击的 wiki 链接按钮。 */
  function restoreWikiLinks(html: string): string {
    return html.replace(/〈WIKI_LINK_(\d+)〉/g, (_m, idx: string) => {
      const title = wikiLinks[parseInt(idx)];
      if (title === undefined) return '';
      const escaped = escapeHtml(title);
      return `<button class="wiki-detail__rel-link" data-rel-title="${escaped}" type="button">[[${escaped}]]</button>`;
    });
  }

  // DOMPurify 兜底：浏览器环境才跑（单测 happy-dom 也算浏览器环境）。
  // micromark 已经 escape，这里是第二层防线，防御未来加 directive / raw html 扩展时的回归。
  if (hasDom()) {
    try {
      const sanitized = buildSanitizer().sanitize(withClasses, {
        // 白名单显式声明：代码块复制按钮（button + data-copy）、KaTeX/MathML 标签放行。
        // KaTeX 输出大量 MathML 标签 + 带 class 的 span/div，这里一并放行；
        // KEEP_CONTENT 默认 true，被过滤标签的内容会保留，不会丢公式文字。
        ALLOWED_TAGS: [
          'p', 'br', 'strong', 'em', 'del', 's', 'code', 'pre', 'span',
          'a', 'ul', 'ol', 'li', 'blockquote', 'hr',
          'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
          'table', 'thead', 'tbody', 'tr', 'th', 'td',
          'div', 'input', 'button',
          ...(options.allowImages ? ['img'] : []),
          // KaTeX 容器
          'math', 'semantics', 'annotation', 'annotation-encoding',
          // MathML 表现元素
          'mrow', 'mi', 'mo', 'mn', 'ms', 'mtext', 'mspace',
          'msup', 'msub', 'msubsup', 'mfrac', 'msqrt', 'mroot',
          'mtable', 'mtr', 'mtd', 'menclose', 'munder', 'mover',
          'munderover', 'mstyle', 'merror', 'mpadded', 'mphantom',
          'mfenced',
        ],
        ALLOWED_ATTR: [
          'href', 'rel', 'target', 'class', 'checked', 'disabled', 'type', 'data-copy',
          'data-mermaid',
          ...(options.allowImages ? ['src', 'alt', 'title', 'loading', 'decoding'] : []),
          // KaTeX / MathML 属性
          'aria-hidden', 'role', 'encoding', 'mathvariant', 'stretchy',
          'fence', 'separator', 'lspace', 'rspace', 'linethickness',
          'columnalign', 'rowalign', 'columnspacing', 'rowspacing',
          'columnspan', 'rowspan', 'align', 'notation', 'depth',
          'height', 'width', 'voffset', 'form', 'char', 'bevelled',
          'denomalign', 'numalign', 'scriptlevel', 'displaystyle',
          'frame', 'framespacing', 'equalrows', 'equalcolumns',
          'side', 'maxsize', 'minsize',
        ],
      });
      return restoreWikiLinks(sanitized);
    } catch {
      // sanitize 失败时退回带 class 的原始输出（micromark 已 escape，仍然安全）。
      return restoreWikiLinks(withClasses);
    }
  }
  // node 纯逻辑环境（无 DOMPurify）——micromark 已 escape，可直接返回。
  return restoreWikiLinks(withClasses);
}

/**
 * 流式渲染专用入口：显式标记 isStreaming=true。
 * 调用方（patchStreamingTurn）用这个；非流式回放用 renderMarkdownHtml。
 * 单独导出是为了让「流式 vs 非流式」在调用点显式可见，不靠隐式默认值。
 */
export function renderMarkdownHtmlStreaming(source: string): string {
  return renderMarkdownHtml(source, { isStreaming: true });
}
