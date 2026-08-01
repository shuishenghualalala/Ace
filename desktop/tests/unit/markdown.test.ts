/**
 * renderMarkdownHtml 单测。
 * 覆盖常见语法 + XSS 注入防护（escapeHtml 在 markdown 处理前先作用）。
 */
import { describe, it, expect } from 'vitest';
import { renderMarkdownHtml } from '../../src/ui/markdown';

describe('renderMarkdownHtml', () => {
  it('empty / whitespace → empty string', () => {
    expect(renderMarkdownHtml('')).toBe('');
    expect(renderMarkdownHtml('   \n\t ')).toBe('');
  });

  it('plain text wrapped in <p>', () => {
    expect(renderMarkdownHtml('hello world')).toBe('<p>hello world</p>');
  });

  it('bold', () => {
    expect(renderMarkdownHtml('**bold**')).toContain('<strong>bold</strong>');
  });

  it('italic', () => {
    expect(renderMarkdownHtml('*it*')).toContain('<em>it</em>');
  });

  it('inline code', () => {
    expect(renderMarkdownHtml('use `foo`')).toContain('<code>foo</code>');
  });

  it('fenced code block with lang', () => {
    const out = renderMarkdownHtml('```js\nconsole.log(1)\n```');
    expect(out).toContain('code-block-wrapper');
    expect(out).toContain('code-block-lang">js');
    expect(out).toContain('console.log(1)');
    expect(out).toContain('code-block-copy');
    expect(out).toContain('data-copy');
  });

  it('streaming C++ fence renders as code block before closing fence arrives', () => {
    const out = renderMarkdownHtml('日历 C++ 伪代码实现\n\n```cpp\n#include <iostream>\nint *p = 0;', { isStreaming: true });
    expect(out).toContain('日历 C++ 伪代码实现');
    expect(out).toContain('code-block-wrapper');
    expect(out).toContain('code-block-lang">cpp');
    expect(out).toContain('#include &lt;iostream&gt;');
    expect(out).toContain('int *p = 0;');
  });

  it('http link opens in new tab', () => {
    const out = renderMarkdownHtml('[click](https://example.com)');
    expect(out).toContain('<a href="https://example.com"');
    expect(out).toContain('target="_blank"');
    expect(out).toContain('rel="noopener noreferrer"');
  });

  it('non-http link href sanitized (javascript: dropped)', () => {
    // micromark + DOMPurify 把 javascript: 这类危险协议清空为 href=""，
    // 不再透传原始 javascript:alert(1) 字符串。旧实现替换成 href="#"，
    // 新实现更严格（空 href），安全性等价。
    const out = renderMarkdownHtml('[x](javascript:alert(1))');
    expect(out).not.toContain('javascript:alert(1)');
    // href 要么为空，要么被改成安全值——只要不含危险协议即可。
    expect(out).toMatch(/href=(""|#)/);
  });

  it('headings h1/h2/h3', () => {
    expect(renderMarkdownHtml('# T1')).toContain('<h1>T1</h1>');
    expect(renderMarkdownHtml('## T2')).toContain('<h2>T2</h2>');
    expect(renderMarkdownHtml('### T3')).toContain('<h3>T3</h3>');
  });

  it('unordered list', () => {
    const out = renderMarkdownHtml('- a\n- b');
    expect(out).toContain('<ul>');
    expect(out).toContain('<li>a</li>');
    expect(out).toContain('<li>b</li>');
  });

  it('horizontal rule (--- / *** / ___)', () => {
    expect(renderMarkdownHtml('a\n\n---\n\nb')).toContain('<hr');
    expect(renderMarkdownHtml('***')).toContain('<hr');
    expect(renderMarkdownHtml('___')).toContain('<hr');
  });

  it('GFM pipe table → thead/tbody', () => {
    const out = renderMarkdownHtml('| 技能 | 示例 |\n|:--|:--|\n| /directory-search | 查电话 |\n| /mail-assistant | 发邮件 |');
    // micromark 直出 <table>，CSS 用 `.chat-markdown table` 覆盖，不再注入 class。
    expect(out).toContain('<table>');
    // micromark 输出 GFM 标准 thead/tbody（带 align 属性、可换行），用关键子串校验而非整体字面匹配。
    expect(out).toContain('<thead>');
    expect(out).toContain('<th');
    expect(out).toContain('技能');
    expect(out).toContain('示例');
    expect(out).toContain('<tbody>');
    expect(out).toContain('查电话');
    expect(out).toContain('发邮件');
    // 表格不应被段落 <br> 串接
    expect(out).not.toContain('<td>查电话</td></tr></tbody></table><br>');
  });

  it('normalizes compact one-line GFM tables from LLM output', () => {
    const out = renderMarkdownHtml('| 市场 | 价格 | 单位 | |------|------|------| 国际现货白银 | 57.49 | 美元/盎司 | 沪银主力 | 13,971 - 13975 | 元/千克 |');
    expect(out).toContain('<table>');
    expect(out).toContain('国际现货白银');
    expect(out).toContain('沪银主力');
    expect(out).not.toContain('|------|------|------|');
  });

  it('inline formatting survives inside table cells', () => {
    const out = renderMarkdownHtml('| 名 | 链 |\n|--|--|\n| `code` | [x](https://e.com) |');
    expect(out).toContain('<td><code>code</code></td>');
    expect(out).toContain('href="https://e.com"');
  });

  it('escapes raw HTML (XSS injection prevented)', () => {
    const out = renderMarkdownHtml('<script>alert(1)</script>');
    expect(out).not.toContain('<script>');
    expect(out).not.toContain('</script>');
    expect(out).toContain('&lt;script&gt;');
  });

  it('escapes HTML inside code block (no execution)', () => {
    const out = renderMarkdownHtml('```\n<b>x</b>\n```');
    expect(out).not.toContain('<b>x</b>');
  });

  // ---- 新增：micromark + GFM 扩展支持的语法（旧正则实现不支持）----

  it('blockquote 渲染成 <blockquote>', () => {
    const out = renderMarkdownHtml('> 这是引用\n> 第二行');
    expect(out).toContain('<blockquote>');
    expect(out).toContain('这是引用');
  });

  it('有序列表渲染成 <ol>', () => {
    const out = renderMarkdownHtml('1. 第一\n2. 第二\n3. 第三');
    expect(out).toContain('<ol>');
    expect(out).toContain('第一');
    expect(out).toContain('第二');
    expect(out).toContain('第三');
  });

  it('嵌套列表（无序嵌套有序）', () => {
    const out = renderMarkdownHtml('- 外层\n  1. 内层一\n  2. 内层二');
    expect(out).toContain('<ul>');
    expect(out).toContain('<ol>');
    expect(out).toContain('内层一');
  });

  it('GFM 删除线 ~~text~~', () => {
    const out = renderMarkdownHtml('~~删除我~~');
    expect(out).toContain('<del>删除我</del>');
  });

  it('GFM 任务列表 [ ] / [x]', () => {
    const out = renderMarkdownHtml('- [x] 已完成\n- [ ] 未完成');
    // micromark-extension-gfm 输出 <input ...> 任务列表项
    expect(out).toContain('checked');
    expect(out).toContain('已完成');
    expect(out).toContain('未完成');
  });

  it('autolink 裸 URL', () => {
    const out = renderMarkdownHtml('参见 https://example.com 了解详情');
    // GFM autolink 会把裸 URL 转成 <a>
    expect(out).toContain('href="https://example.com"');
  });

  it('流式：半截 **bold 自动闭合（不显示源码）', () => {
    // 模拟流式中：**bold 还没收到闭合 **
    const out = renderMarkdownHtml('这是 **正在加粗', { isStreaming: true });
    // 应该渲染成粗体（自动闭合），而不是显示原始 **
    expect(out).not.toContain('**正在加粗');
    expect(out).toContain('<strong>');
  });

  it('流式：有序列表项里的半截 **bold 自动闭合', () => {
    const out = renderMarkdownHtml([
      '1. **触发条件** — 记忆复盘',
      '2. **F2. Fork 机制 — 复制子 Agent 独立复盘',
    ].join('\n'), { isStreaming: true });
    expect(out).toContain('<strong>F2. Fork 机制');
    expect(out).not.toContain('**F2. Fork');
  });

  it('流式：空加粗开头不闪出 ****', () => {
    const out = renderMarkdownHtml('- 📝 **', { isStreaming: true });
    expect(out).not.toContain('****');
  });

  it('流式：未闭合 ```python fence 自动闭合', () => {
    const out = renderMarkdownHtml('```python\nprint(hi)', { isStreaming: true });
    // 应该渲染成代码块，而不是把 ```python 当纯文本显示。
    // 注意：micromark 会 escape 内容里的特殊字符，所以用 code-block-wrapper + print 作为存在性断言。
    expect(out).toContain('code-block-wrapper');
    expect(out).toContain('print(hi)');
  });

  // ---- 数学公式（micromark-extension-math + KaTeX）----

  it('inline math $...$ 渲染成 KaTeX', () => {
    const out = renderMarkdownHtml('公式 $E=mc^2$ 在文中');
    // KaTeX 输出带 katex class 的 span
    expect(out).toContain('katex');
    expect(out).toContain('E=mc');
  });

  it('block math $$...$$ 渲染成 KaTeX display', () => {
    const out = renderMarkdownHtml('$$\nE=mc^2\n$$');
    expect(out).toContain('katex');
    expect(out).toContain('math-display');
  });

  it('流式：未闭合 $$ 自动闭合（不显示源码）', () => {
    const out = renderMarkdownHtml('$$\nE=mc^2', { isStreaming: true });
    expect(out).toContain('katex');
    expect(out).not.toMatch(/\$\$\s*$/);
  });

  // ---- Mermaid 图表（占位 + 懒加载渲染）----

  it('mermaid 代码块包成 data-mermaid 占位 div', () => {
    const out = renderMarkdownHtml('```mermaid\ngraph TD\nA-->B\n```');
    expect(out).toContain('class="mermaid"');
    expect(out).toContain('data-mermaid');
    // 不应走普通代码块 wrapper
    expect(out).not.toContain('code-block-wrapper');
    expect(out).toContain('A--&gt;B');
  });

  it('普通代码块（非 mermaid）仍走 code-block-wrapper', () => {
    const out = renderMarkdownHtml('```js\nconsole.log(1)\n```');
    expect(out).toContain('code-block-wrapper');
    expect(out).not.toContain('data-mermaid');
  });

  // ---- Wiki 链接 [[名称]] 渲染为可点击按钮 ----

  it('[[Wiki 链接]] 渲染为可点击按钮', () => {
    const out = renderMarkdownHtml('- [[Trip.com]]\n- [[埃及航空 (EgyptAir)]]');
    expect(out).toContain('data-rel-title="Trip.com"');
    expect(out).toContain('data-rel-title="埃及航空 (EgyptAir)"');
    expect(out).toContain('class="wiki-detail__rel-link"');
    expect(out).not.toContain('WIKI_LINK');
    expect(out).not.toContain('〈');
    expect(out).not.toContain('〉');
  });

  it('普通文本中的 [[ 不被误处理（无闭合）', () => {
    const out = renderMarkdownHtml('这是一段带有 [[ 的文本，但没有闭合');
    expect(out).not.toContain('data-rel-title');
    expect(out).toContain('[[ 的文本');
  });
});
