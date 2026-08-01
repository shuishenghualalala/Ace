/**
 * markdown-stream 单测：流式预处理对未完成语法的自动闭合。
 */
import { describe, it, expect } from 'vitest';
import { preprocessStreamMarkdown } from '../../src/ui/markdown-stream';

describe('preprocessStreamMarkdown', () => {
  it('完整 markdown 不变（幂等）', () => {
    const src = '**bold** and *italic* and `code`';
    expect(preprocessStreamMarkdown(src, true)).toBe(src);
  });

  it('未闭合的 ** 在末尾补一个', () => {
    const out = preprocessStreamMarkdown('这是 **bold 还没结束', true);
    // 末尾应该补了一个 **，使计数变偶
    const stars = (out.match(/\*\*/g) ?? []).length;
    expect(stars % 2).toBe(0);
  });

  it('流式停在空 ** 开头时先隐藏标记，避免闪出 ****', () => {
    const out = preprocessStreamMarkdown('- 📝 **', true);
    expect(out).toBe('- 📝 ');
    expect(out).not.toContain('****');
  });

  it('未闭合的 ``` 在末尾补闭合行', () => {
    const out = preprocessStreamMarkdown('```python\nprint("hello")', true);
    // 应该在末尾补上 ```
    expect(out.endsWith('```')).toBe(true);
  });

  it('已闭合的 ``` 不被多补', () => {
    const src = '```python\nprint(1)\n```';
    const out = preprocessStreamMarkdown(src, true);
    // 原本 2 个 ```，处理后应该还是 2 个（不补）
    const fences = (out.match(/```/g) ?? []).length;
    expect(fences).toBe(2);
  });

  it('不在未闭合代码围栏内部补强调或数学定界符', () => {
    const src = '```cpp\nint *p = 0;\ncout << "$";';
    const out = preprocessStreamMarkdown(src, true);
    expect(out).toBe(src + '\n```');
  });

  it('完整代码围栏内部的 markdown 字符保持原样', () => {
    const src = '```cpp\nint *p = 0;\ncout << "$";\n```';
    expect(preprocessStreamMarkdown(src, true)).toBe(src);
  });

  it('多块代码中只有最后一块未闭合时只补最后一块', () => {
    const src = '```js\na()\n```\n\n文字\n\n```python\nb()';
    const out = preprocessStreamMarkdown(src, true);
    // 输入有 3 个 ```（js-open, js-close, python-open），自动闭合后变 4 个。
    const fences = (out.match(/```/g) ?? []).length;
    expect(fences).toBe(4);
    expect(out.endsWith('```')).toBe(true);
  });

  it('inline code 内的 * 不参与强调配对', () => {
    // `a*b` 里的 * 是字面量，不应该被算进强调统计。
    // 这条用 `**` 配对：外面有一个未闭合的 **，inline code 里的 * 不影响。
    const src = '未闭合 **bold 和 `a*b` 继续';
    const out = preprocessStreamMarkdown(src, true);
    const stars = (out.match(/\*\*/g) ?? []).length;
    expect(stars % 2).toBe(0);
  });

  it('空字符串原样返回', () => {
    expect(preprocessStreamMarkdown('', true)).toBe('');
  });

  it('无强调无代码的纯文本不变', () => {
    const src = '这是一段普通文字，没有任何 markdown 语法。';
    expect(preprocessStreamMarkdown(src, true)).toBe(src);
  });

  it('单词内的 _ 不被误判为强调（a_b 不补 _）', () => {
    // CommonMark：_ 在单词内部不触发强调。技术标识符 my_var_name 不应被补成 my_var_name_。
    const src = '调用 my_var_name 函数';
    const out = preprocessStreamMarkdown(src, true);
    expect(out).toBe(src); // 不应补 _
    expect(out.endsWith('_')).toBe(false);
  });

  it('真正的 _ 强调（词边界）仍会被闭合', () => {
    // _italic_ 这种 _ 前后是空格/标点（词边界），是真正的强调，未闭合应补。
    const src = '这是 _italic 未闭合';
    const out = preprocessStreamMarkdown(src, true);
    // 末尾应补一个 _（前面可能加空格）
    expect(out.endsWith('_')).toBe(true);
    const underscores = (out.match(/(?<![A-Za-z0-9_])_|_(?![A-Za-z0-9_])/g) ?? []).length;
    expect(underscores % 2).toBe(0);
  });

  it('未闭合的 $$ 在末尾补 $$（单独成行）', () => {
    const out = preprocessStreamMarkdown('公式：$$E=mc^2', true);
    // block math 闭合 $$ 需单独成行，所以末尾是 \n$$
    expect(out.endsWith('\n$$')).toBe(true);
    const dquotes = (out.match(/\$\$/g) ?? []).length;
    expect(dquotes % 2).toBe(0);
  });

  it('未闭合的 $ 在末尾补 $', () => {
    const out = preprocessStreamMarkdown('inline $E=mc^2 公式', true);
    expect(out.endsWith('$')).toBe(true);
  });

  it('已闭合的 $$ 不被多补', () => {
    const src = '$$E=mc^2$$';
    const out = preprocessStreamMarkdown(src, true);
    const dquotes = (out.match(/\$\$/g) ?? []).length;
    expect(dquotes).toBe(2);
    expect(out).toBe(src);
  });

  it('inline code 内的 $ 不参与配对', () => {
    // `a$b` 里的 $ 是字面量，外面的 $ 未闭合应被补。
    const src = '价格 `$5` 起，未闭合 $x';
    const out = preprocessStreamMarkdown(src, true);
    // 末尾补了一个 $（code 内的 $ 不算）
    expect(out.endsWith('$')).toBe(true);
  });
});
