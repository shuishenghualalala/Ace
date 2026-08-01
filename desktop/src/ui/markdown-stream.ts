/**
 * 流式 Markdown 预处理：在 micromark 解析前修复「未完成的语法」，
 * 让流式中半截 `**bold` / ` ```python ` 不再以源码形式闪烁出现。
 *
 * 这里只做「自动闭合」，不做 autolink / 货币转义 / LaTeX 重写等 polish（按最小闭环原则）。
 *
 * 纯字符串处理，无 DOM 依赖，可在 node 环境单测。
 */

/**
 * 把流式中可能未闭合的围栏代码块、强调标记、数学公式定界符自动闭合。
 *
 * @param source 流式当前累积的 markdown 源文本
 * @param isStreaming 是否仍处于流式状态。已结束的回合也调用本函数无害（输入已平衡，返回原值）。
 * @returns 可安全交给 markdown 解析器的源文本
 */
export function preprocessStreamMarkdown(source: string, isStreaming: boolean): string {
  if (!source) return source;
  // 即使非流式也跑一遍——对完整 markdown 是幂等的，对历史回放无副作用。
  // 之所以不短路，是为了让「流式末态 → 已完成态」不会因为路径不同产生样式跳变。
  const withClosedFences = closeUnclosedFences(source);
  return mapOutsideFencedCode(withClosedFences, (outside) =>
    closeUnclosedMath(closeUnclosedEmphasis(outside, isStreaming)),
  );
}

/**
 * 自动闭合未闭合的 ``` / ~~~ 围栏。
 * 仅处理「最后一对未闭合」的围栏——多块代码场景下中间块都已成对，
 * 只有正在流式生成的那一块可能未闭合。
 */
function closeUnclosedFences(text: string): string {
  // 扫描所有围栏行，用栈配对；栈里剩下的就是未闭合的开围栏。
  const lines = text.split('\n');
  const stack: { marker: string; indent: string }[] = [];
  // 围栏行：行首可选空白，然后 ``` 或 ~~~（≥3 个），后面可选 info string。
  const fenceRe = /^([ \t]*)(`{3,}|~{3,})([^\n]*)$/;
  for (const line of lines) {
    const m = line.match(fenceRe);
    if (!m) continue;
    const indent = m[1] ?? '';
    const marker = m[2] ?? '';
    const info = (m[3] ?? '').trim();
    // 闭合围栏：info 必须为空，且 marker 同种（同字符）且长度 ≥ 开围栏。
    if (!info && stack.length > 0) {
      const top = stack[stack.length - 1]!;
      if (top.marker[0] === marker[0] && marker.length >= top.marker.length) {
        stack.pop();
        continue;
      }
    }
    // 否则视为开围栏（即使是看起来像闭合但 info 非空的行，micromark 也会当作开围栏）。
    stack.push({ marker, indent });
  }
  if (stack.length === 0) return text;
  // 末尾补上每个未闭合围栏的对应闭合行。用同 marker 同长度（micromark 接受 ≥ 开围栏长度）。
  const closing = stack.map(({ marker, indent }) => `${indent}${marker}`).join('\n');
  return text + '\n' + closing;
}

/**
 * 只转换围栏代码块之外的文本。
 *
 * 流式补全会主动补 `*` / `$` 等 markdown 定界符；代码块里的 C++ 指针、
 * shell 变量、LaTeX 示例都应作为字面量保留，否则流式阶段会显示一份被补全器
 * 改写过的源码，而 final 阶段又恢复为模型原文。
 */
function mapOutsideFencedCode(text: string, transform: (outside: string) => string): string {
  const chunks = text.match(/[^\n]*(?:\n|$)/g) ?? [];
  const fenceRe = /^([ \t]*)(`{3,}|~{3,})([^\n]*)$/;
  let out = '';
  let outside = '';
  let activeFence: { marker: string } | null = null;

  const flushOutside = (): void => {
    if (!outside) return;
    out += transform(outside);
    outside = '';
  };

  for (const chunk of chunks) {
    if (!chunk) continue;
    const line = chunk.endsWith('\n') ? chunk.slice(0, -1) : chunk;
    const match = line.match(fenceRe);

    if (match) {
      const marker = match[2] ?? '';
      const info = (match[3] ?? '').trim();
      if (activeFence) {
        out += chunk;
        if (!info && activeFence.marker[0] === marker[0] && marker.length >= activeFence.marker.length) {
          activeFence = null;
        }
        continue;
      }

      flushOutside();
      activeFence = { marker };
      out += chunk;
      continue;
    }

    if (activeFence) {
      out += chunk;
    } else {
      outside += chunk;
    }
  }

  flushOutside();
  return out;
}

/**
 * 自动闭合未闭合的 `**` / `__` / `*` / `_` 强调标记。
 *
 * 算法：统计每个标记在「非代码段」内的未配对出现次数，奇数则末尾补一个。
 * 代码段（`...`）内的标记不参与配对——它们是字面量。
 *
 * `_` 特殊处理：CommonMark 规定 `_` 在单词内部不触发强调（`a_b` 不是加粗/斜体），
 * 所以统计 `_` 时排除「前后都是字母数字」的出现，避免把技术标识符 `my_var_name`
 * 误判为奇数、补成 `my_var_name_` 导致流式末尾出现意外 `<em>`。
 * `*` 不受此限——`*` 在词中间也可触发强调（如 `a*b*c`），保持原行为。
 *
 * 这是近似实现，不处理跨段落的复杂嵌套，但对 LLM 流式输出（绝大多数强调是单段内的）足够。
 */
function closeUnclosedEmphasis(text: string, isStreaming: boolean): string {
  // 拆出 `inline code` 段，对其外部分统计强调标记。
  const segments = text.split(/(`[^`\n]+`)/g);
  // 偶数下标 = 非代码段；奇数下标 = 代码段（保留原样）。
  for (let i = 0; i < segments.length; i += 2) {
    const seg = segments[i];
    if (!seg) continue;
    segments[i] = balanceEmphasisRun(seg, isStreaming);
  }
  return segments.join('');
}

/** 在单段文本里平衡 `**`、`__`、`*`、`_` 四种强调标记。 */
function balanceEmphasisRun(text: string, isStreaming: boolean): string {
  // 顺序很重要：先处理 `**` / `__`（双字符），再处理 `*` / `_`（单字符），
  // 否则 `**` 会被当成两个 `*`。
  let out = text;
  out = balanceMarker(out, '**', isStreaming);
  out = balanceMarker(out, '__', isStreaming);
  out = balanceMarker(out, '*', isStreaming);
  out = balanceMarker(out, '_', isStreaming);
  return out;
}

/**
 * 统计 marker 在文本中的「未配对」出现次数；奇数则末尾补一个。
 *
 * 对 `_` 单字符标记，排除「单词内部」的出现（前后是字母数字/下划线），
 * 与 CommonMark 的 `_` 强调规则一致。`**`/`__`/`*` 不做此排除。
 */
function balanceMarker(text: string, marker: string, isStreaming: boolean): string {
  // 转义 marker 用于正则。
  const esc = marker.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  if (marker === '_') {
    // 只统计「不在单词内部」的 `_`：前一个字符或后一个字符不是字母数字/下划线才算强调标记。
    // 用否定环视边界匹配。流式中末尾的 `_` 后面无字符，也算强调标记（后边界满足）。
    const re = /(?<![A-Za-z0-9_])_|_(?![A-Za-z0-9_])/g;
    const count = (text.match(re) ?? []).length;
    if (count % 2 === 0) return text;
    if (isStreaming && text.endsWith('_')) return text.slice(0, -1);
    return text + '_';
  }
  // `**` / `__` / `*`：直接 count 所有出现次数。
  const re = new RegExp(esc, 'g');
  const count = (text.match(re) ?? []).length;
  if (count % 2 === 0) return text;
  if (isStreaming && text.endsWith(marker)) return text.slice(0, -marker.length);
  // 奇数 → 末尾补一个 marker（前面加一个空格防止粘连到词尾被解析为不同语义）。
  // 注意：流式中末尾往往就是「正在生成的位置」，补在这里视觉影响最小。
  return text + marker;
}

/**
 * 自动闭合未配对的 `$` / `$$` 数学公式定界符。
 *
 * micromark-extension-math 的规则：
 *  - `$$...$$` 是 block math（display mode），`$...$` 是 inline math。
 *  - 流式中末尾可能出现 `$$E=mc^2`（未闭合 block）或 `$E=mc^2`（未闭合 inline），
 *    不补的话 micromark 会把整段当普通文本，公式以源码形式闪烁出现。
 *
 * 算法：先处理 `$$`（双美元），再处理 `$`（单美元）。
 *  - 统计 `$$` 出现次数，奇数则末尾补 `$$`。
 *  - 再统计剩余的「不成对 `$`」（即不在 `$$` 里的单 `$`），奇数则末尾补 `$`。
 *
 * 注意：代码块/inline code 内的 `$` 是字面量，不该参与配对。
 * 但本函数在 closeUnclosedFences 之后跑——围栏已闭合，micromark 会把代码块内容当字面量，
 * 代码块内的 `$` 即使被我们多补了一个 `$`，也在闭合的代码块里，不影响渲染。
 * inline code 的 `$` 需要排除，这里用类似 emphasis 的拆分处理。
 */
function closeUnclosedMath(text: string): string {
  // 先拆出 `inline code` 段，只对非代码段统计 $。
  const segments = text.split(/(`[^`\n]+`)/g);
  for (let i = 0; i < segments.length; i += 2) {
    const seg = segments[i];
    if (!seg) continue;
    segments[i] = balanceMathRun(seg);
  }
  return segments.join('');
}

/** 在单段非代码文本里平衡 $$ 和 $。 */
function balanceMathRun(text: string): string {
  // 先处理 $$：统计出现次数，奇数补 $$。
  // block math 的闭合 $$ 必须单独成行（micromark-extension-math 的 mathFlow 语法），
  // 所以补的时候要先确保换行：去掉末尾空白 + '\n' + '$$'。
  // 否则 $$E=mc^2$$ 会被 micromark 当成内容里含 $$，KaTeX 报 '$' in math mode。
  const dquotes = (text.match(/\$\$/g) ?? []).length;
  let out = text;
  if (dquotes % 2 !== 0) {
    out = out.replace(/\s+$/, '') + '\n$$';
  }
  // 再处理单 $：把已有的 $$ 抠除后统计剩余 $。
  // 用 replace 把 $$ 替成占位再数 $，避免 $$ 被当成两个 $。
  const withoutDd = out.replace(/\$\$/g, '');
  const singles = (withoutDd.match(/\$/g) ?? []).length;
  if (singles % 2 !== 0) {
    out = out + '$';
  }
  return out;
}
