/**
 * Unified diff 解析与 Inspector 渲染辅助。
 *
 * 借鉴 Hermes `diff-lines.tsx`：去掉 git 头与 `@@` 噪音、靠色条区分增删、
 * 追踪新旧行号，并对长段未改动上下文做折叠（Cursor 风格）。
 */

export type DiffKind = 'add' | 'context' | 'remove';

export interface DiffLine {
  kind: DiffKind;
  text: string;
  /** 旧文件 1-based 行号（删除行 / 上下文行）。 */
  oldNo?: number;
  /** 新文件 1-based 行号（新增行 / 上下文行）。 */
  newNo?: number;
}

export interface BackendDiffRow {
  line: number;
  kind: 'meta' | 'ctx' | 'add' | 'del';
  text: string;
}

export type DisplayDiffItem =
  | { type: 'line'; line: DiffLine }
  | {
      type: 'collapsed';
      /** 在完整 DiffLine[] 中的起止下标 [start, end)。 */
      start: number;
      end: number;
      count: number;
    };

/** 某段折叠区已从顶部 / 底部揭开的行数（按 region start 索引）。 */
export type DiffRegionExpandMap = Record<number, { top: number; bottom: number }>;

const DIFF_HEADER_PREFIXES = [
  'diff --git',
  'index ',
  '--- ',
  '+++ ',
  'similarity ',
  'rename ',
  'new file',
  'deleted file',
];

/** 超过该行数的连续未改动上下文折叠为 unmodified 条。 */
export const CONTEXT_COLLAPSE_THRESHOLD = 8;
/** 点击折叠条上下箭头时每次揭开的行数。 */
export const CONTEXT_EXPAND_CHUNK = 20;
const SLOT = '\uE000';

function isArrowHeaderLine(line: string): boolean {
  const trimmed = line.trim();
  return trimmed.includes('→') && /^\S.*→\s*\S+$/.test(trimmed) && !/^[+\-@]/.test(trimmed);
}

/** 去掉 unified diff 文件头，保留从首个 hunk 起的内容。 */
export function stripDiffFileHeaders(diff: string): string {
  const lines = diff.split('\n');
  let start = 0;

  for (; start < lines.length; start += 1) {
    const line = lines[start];
    if (line.startsWith('@@')) break;
    if (
      line.trim() === ''
      || isArrowHeaderLine(line)
      || DIFF_HEADER_PREFIXES.some((prefix) => line.startsWith(prefix))
    ) {
      continue;
    }
    break;
  }

  return lines.slice(start).join('\n');
}

function diffKindFromMarker(line: string): DiffKind {
  if (line.startsWith('+') && !line.startsWith('+++')) return 'add';
  if (line.startsWith('-') && !line.startsWith('---')) return 'remove';
  return 'context';
}

function stripDiffMarker(line: string): string {
  if (diffKindFromMarker(line) !== 'context' || line.startsWith(' ')) {
    return line.slice(1);
  }
  return line;
}

interface ParsedHunk {
  lines: Array<{ kind: DiffKind; text: string }>;
  newStart: number;
  oldStart: number;
}

function parseHunks(diff: string): ParsedHunk[] {
  const hunks: ParsedHunk[] = [];
  let active: ParsedHunk | null = null;

  for (const line of stripDiffFileHeaders(diff).split('\n')) {
    if (line.startsWith('@@')) {
      const match = /@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(line);
      if (!match) {
        active = null;
        continue;
      }
      active = { oldStart: Number(match[1]), newStart: Number(match[2]), lines: [] };
      hunks.push(active);
      continue;
    }

    if (!active || line.startsWith('\\')) continue;
    active.lines.push({ kind: diffKindFromMarker(line), text: stripDiffMarker(line) });
  }

  return hunks;
}

/** 将 unified diff 文本解析为可渲染行（含行号）。 */
export function parseDiffText(diff: string): DiffLine[] {
  const hunks = parseHunks(diff);

  if (hunks.length === 0) {
    return stripDiffFileHeaders(diff)
      .split('\n')
      .filter((line) => !line.startsWith('@@'))
      .map((line) => ({ kind: diffKindFromMarker(line), text: stripDiffMarker(line) }));
  }

  const out: DiffLine[] = [];
  let emitted = false;
  let oldNo = 1;
  let newNo = 1;

  for (const hunk of hunks) {
    oldNo = hunk.oldStart;
    newNo = hunk.newStart;

    if (emitted) {
      out.push({ kind: 'context', text: '' });
    }

    for (const line of hunk.lines) {
      const entry: DiffLine = { kind: line.kind, text: line.text };

      if (line.kind === 'add') {
        entry.newNo = newNo++;
      } else if (line.kind === 'remove') {
        entry.oldNo = oldNo++;
      } else {
        entry.oldNo = oldNo++;
        entry.newNo = newNo++;
      }

      out.push(entry);
      emitted = true;
    }
  }

  return out;
}

function backendKindToDiffKind(kind: BackendDiffRow['kind']): DiffKind | null {
  if (kind === 'add') return 'add';
  if (kind === 'del') return 'remove';
  if (kind === 'ctx') return 'context';
  return null;
}

function isSkippableMeta(text: string): boolean {
  const t = text.trim();
  if (t.startsWith('@@')) return false;
  if (t.startsWith('---') || t.startsWith('+++')) return true;
  if (DIFF_HEADER_PREFIXES.some((prefix) => t.startsWith(prefix))) return true;
  return isArrowHeaderLine(t);
}

/** 将后端 `file_changes` 帧里的 DiffRow 列表转为可渲染行。 */
export function parseBackendDiffRows(rows: BackendDiffRow[]): DiffLine[] {
  const out: DiffLine[] = [];
  let oldNo = 1;
  let newNo = 1;
  let emitted = false;

  for (const row of rows) {
    if (row.kind === 'meta') {
      const match = /@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(row.text);
      if (match) {
        oldNo = Number(match[1]);
        newNo = Number(match[2]);
        if (emitted) {
          out.push({ kind: 'context', text: '' });
        }
      }
      continue;
    }

    if (isSkippableMeta(row.text)) continue;

    const kind = backendKindToDiffKind(row.kind);
    if (!kind) continue;

    const entry: DiffLine = { kind, text: row.text };

    if (kind === 'add') {
      entry.newNo = newNo++;
    } else if (kind === 'remove') {
      entry.oldNo = oldNo++;
    } else {
      entry.oldNo = oldNo++;
      entry.newNo = newNo++;
    }

    out.push(entry);
    emitted = true;
  }

  return out;
}

/**
 * 将长段未改动上下文折叠为 unmodified 占位。
 * `expands[start]` 记录该段已从顶部 / 底部揭开的行数（每次箭头 +CONTEXT_EXPAND_CHUNK）。
 */
export function collapseContextRuns(
  lines: DiffLine[],
  threshold = CONTEXT_COLLAPSE_THRESHOLD,
  expands: DiffRegionExpandMap = {},
): DisplayDiffItem[] {
  const out: DisplayDiffItem[] = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];
    if (line.kind !== 'context' || line.text === '') {
      out.push({ type: 'line', line });
      i += 1;
      continue;
    }

    let j = i;
    while (j < lines.length && lines[j].kind === 'context' && lines[j].text !== '') {
      j += 1;
    }

    const runLen = j - i;
    const revealed = expands[i] ?? { top: 0, bottom: 0 };
    const top = Math.max(0, Math.min(revealed.top, runLen));
    const bottom = Math.max(0, Math.min(revealed.bottom, runLen - top));
    const hiddenStart = i + top;
    const hiddenEnd = j - bottom;
    const hiddenCount = hiddenEnd - hiddenStart;

    for (let k = i; k < hiddenStart; k += 1) {
      out.push({ type: 'line', line: lines[k] });
    }

    if (hiddenCount > threshold) {
      out.push({ type: 'collapsed', start: i, end: j, count: hiddenCount });
    } else {
      for (let k = hiddenStart; k < hiddenEnd; k += 1) {
        out.push({ type: 'line', line: lines[k] });
      }
    }

    for (let k = hiddenEnd; k < j; k += 1) {
      out.push({ type: 'line', line: lines[k] });
    }
    i = j;
  }

  return out;
}

/** 折叠区顶部 / 底部再揭开 chunk 行；返回下一份 expands（不可变）。 */
export function expandCollapsedRegion(
  expands: DiffRegionExpandMap,
  regionStart: number,
  edge: 'top' | 'bottom',
  chunk = CONTEXT_EXPAND_CHUNK,
): DiffRegionExpandMap {
  const prev = expands[regionStart] ?? { top: 0, bottom: 0 };
  const next = {
    top: edge === 'top' ? prev.top + chunk : prev.top,
    bottom: edge === 'bottom' ? prev.bottom + chunk : prev.bottom,
  };
  return { ...expands, [regionStart]: next };
}

/**
 * 文件卡折叠时清除该路径的临时 unmodified 展开进度。
 * 再展开时回到默认折叠态（「临时」语义）。
 */
export function clearDiffExpandsForPath(
  expandsByPath: Map<string, DiffRegionExpandMap>,
  path: string,
): void {
  if (path) expandsByPath.delete(path);
}

function diffSignForKind(kind: DiffKind): string {
  if (kind === 'add') return '+';
  if (kind === 'remove') return '−';
  return ' ';
}

/** 从 before/after 全文生成后端兼容的 DiffRow（用于磁盘回读 / 参数回退）。 */
export function buildDiffFromTexts(
  before: string | null | undefined,
  after: string,
  limit = 800,
): BackendDiffRow[] {
  const beforeLines = (before ?? '').split('\n');
  const afterLines = after.split('\n');
  const rows: BackendDiffRow[] = [];

  if (before == null || before === '') {
    for (const line of afterLines) {
      rows.push({ line: 0, kind: 'add', text: line });
      if (rows.length >= limit) break;
    }
    return rows;
  }

  const maxLen = Math.max(beforeLines.length, afterLines.length);
  for (let i = 0; i < maxLen && rows.length < limit; i += 1) {
    const b = beforeLines[i];
    const a = afterLines[i];
    if (b === a) {
      if (a !== undefined) rows.push({ line: 0, kind: 'ctx', text: a });
    } else {
      if (b !== undefined) rows.push({ line: 0, kind: 'del', text: b });
      if (a !== undefined) rows.push({ line: 0, kind: 'add', text: a });
    }
  }
  return rows;
}

export function countDiffRows(diff: BackendDiffRow[]): { added: number; removed: number } {
  let added = 0;
  let removed = 0;
  for (const row of diff) {
    if (row.kind === 'add') added += 1;
    else if (row.kind === 'del') removed += 1;
  }
  return { added, removed };
}

export interface OverviewRun {
  kind: 'add' | 'remove';
  sizePct: number;
  startPct: number;
}

/** 右侧概览色条：按变更行占比定位增删块。 */
export function buildOverviewRuns(lines: DiffLine[]): OverviewRun[] {
  const total = lines.length || 1;
  const runs: OverviewRun[] = [];

  for (let i = 0; i < lines.length; ) {
    const kind = lines[i].kind;
    if (kind === 'context') {
      i += 1;
      continue;
    }

    let j = i + 1;
    while (j < lines.length && lines[j].kind === kind) {
      j += 1;
    }

    runs.push({
      kind,
      sizePct: ((j - i) / total) * 100,
      startPct: (i / total) * 100,
    });
    i = j;
  }

  return runs;
}

function lineNoForGutter(line: DiffLine): string {
  if (line.kind === 'remove') return line.oldNo != null ? String(line.oldNo) : '';
  return line.newNo != null ? String(line.newNo) : '';
}

function kindClass(kind: DiffKind): string {
  if (kind === 'add') return 'is-add';
  if (kind === 'remove') return 'is-remove';
  return 'is-context';
}

function languageFromFilename(filename?: string): string {
  if (!filename) return 'text';
  const base = filename.split(/[\\/]/).pop() || filename;
  const ext = base.includes('.') ? base.split('.').pop()!.toLowerCase() : '';
  const map: Record<string, string> = {
    html: 'html', htm: 'html', xhtml: 'html',
    css: 'css', scss: 'css', less: 'css',
    js: 'javascript', mjs: 'javascript', cjs: 'javascript',
    ts: 'typescript', tsx: 'typescript', jsx: 'javascript',
    py: 'python', pyw: 'python',
    json: 'json', md: 'markdown', yaml: 'yaml', yml: 'yaml',
    xml: 'xml', sql: 'sql', sh: 'shell', bash: 'shell',
  };
  return map[ext] ?? 'text';
}

/** 在原始文本上插入 slot，最后对非 slot 区间 escape 并还原。 */
function highlightWithSlots(raw: string, esc: (s: string) => string, applyRules: (mark: (html: string) => string) => string): string {
  const slots: string[] = [];
  const mark = (html: string): string => {
    const id = slots.length;
    slots.push(html);
    return `${SLOT}${id}${SLOT}`;
  };
  const marked = applyRules(mark);
  const re = new RegExp(`${SLOT}(\\d+)${SLOT}`, 'g');
  let out = '';
  let last = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(marked)) !== null) {
    out += esc(marked.slice(last, m.index));
    out += slots[Number(m[1])] ?? '';
    last = re.lastIndex;
  }
  out += esc(marked.slice(last));
  return out;
}

function highlightHtmlRaw(raw: string, esc: (s: string) => string): string {
  return highlightWithSlots(raw, esc, (mark) => {
    let s = raw;
    s = s.replace(/<!--[\s\S]*?-->/g, (m) => mark(`<span class="diff-tok diff-tok-comment">${esc(m)}</span>`));
    s = s.replace(/<!DOCTYPE[^>]*>/gi, (m) => mark(`<span class="diff-tok diff-tok-meta">${esc(m)}</span>`));
    s = s.replace(/("[^"\\]*(?:\\.[^"\\]*)*"|'[^'\\]*(?:\\.[^'\\]*)*')/g, (m) => mark(`<span class="diff-tok diff-tok-str">${esc(m)}</span>`));
    s = s.replace(/(<\/?)([a-zA-Z][\w:-]*)/g, (_m, open, name) =>
      mark(`<span class="diff-tok diff-tok-punct">${esc(open)}</span><span class="diff-tok diff-tok-tag">${esc(name)}</span>`));
    s = s.replace(/(\s)([a-zA-Z_:][\w:.-]*)(=)/g, (_m, sp, name, eq) =>
      mark(`${esc(sp)}<span class="diff-tok diff-tok-attr">${esc(name)}</span><span class="diff-tok diff-tok-punct">${esc(eq)}</span>`));
    return s;
  });
}

function highlightCssRaw(raw: string, esc: (s: string) => string): string {
  return highlightWithSlots(raw, esc, (mark) => {
    let s = raw;
    s = s.replace(/\/\*[\s\S]*?\*\//g, (m) => mark(`<span class="diff-tok diff-tok-comment">${esc(m)}</span>`));
    s = s.replace(/("[^"\\]*(?:\\.[^"\\]*)*"|'[^'\\]*(?:\\.[^'\\]*)*')/g, (m) => mark(`<span class="diff-tok diff-tok-str">${esc(m)}</span>`));
    s = s.replace(/([.#][\w-]+)/g, (m) => mark(`<span class="diff-tok diff-tok-tag">${esc(m)}</span>`));
    s = s.replace(/([\w-]+)(\s*:)/g, (_m, name, colon) =>
      mark(`<span class="diff-tok diff-tok-attr">${esc(name)}</span><span class="diff-tok diff-tok-punct">${esc(colon)}</span>`));
    return s;
  });
}

function highlightJsRaw(raw: string, esc: (s: string) => string): string {
  return highlightWithSlots(raw, esc, (mark) => {
    let s = raw;
    s = s.replace(/(\/\/[^\n]*|\/\*[\s\S]*?\*\/)/g, (m) => mark(`<span class="diff-tok diff-tok-comment">${esc(m)}</span>`));
    s = s.replace(/(`[^`\\]*(?:\\.[^`\\]*)*`|"[^"\\]*(?:\\.[^"\\]*)*"|'[^'\\]*(?:\\.[^'\\]*)*')/g, (m) => mark(`<span class="diff-tok diff-tok-str">${esc(m)}</span>`));
    s = s.replace(/\b(const|let|var|function|return|if|else|for|while|import|export|from|class|extends|new|async|await|try|catch|throw|typeof|interface|type)\b/g, (m) =>
      mark(`<span class="diff-tok diff-tok-kw">${esc(m)}</span>`));
    return s;
  });
}

function highlightPythonRaw(raw: string, esc: (s: string) => string): string {
  return highlightWithSlots(raw, esc, (mark) => {
    let s = raw;
    s = s.replace(/(#.*$)/gm, (m) => mark(`<span class="diff-tok diff-tok-comment">${esc(m)}</span>`));
    s = s.replace(/("[^"\\]*(?:\\.[^"\\]*)*"|'[^'\\]*(?:\\.[^'\\]*)*')/g, (m) => mark(`<span class="diff-tok diff-tok-str">${esc(m)}</span>`));
    s = s.replace(/\b(def|class|return|if|elif|else|for|while|import|from|as|try|except|raise|with|async|await|pass|break|continue|True|False|None)\b/g, (m) =>
      mark(`<span class="diff-tok diff-tok-kw">${esc(m)}</span>`));
    return s;
  });
}

function highlightJsonRaw(raw: string, esc: (s: string) => string): string {
  return highlightWithSlots(raw, esc, (mark) => raw.replace(
    /("[^"\\]*(?:\\.[^"\\]*)*")(\s*:)?|\b(true|false|null)\b|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)/g,
    (_m, str: string, colon: string | undefined, bool: string | undefined, num: string | undefined) => {
      if (bool) return mark(`<span class="diff-tok diff-tok-kw">${esc(bool)}</span>`);
      if (num) return mark(`<span class="diff-tok diff-tok-num">${esc(num)}</span>`);
      if (colon) return mark(`<span class="diff-tok diff-tok-attr">${esc(str)}</span><span class="diff-tok diff-tok-punct">${esc(colon)}</span>`);
      return mark(`<span class="diff-tok diff-tok-str">${esc(str)}</span>`);
    },
  ));
}

/** 对原始源码做语法上色（span 内逐段 escape，避免 innerHTML 与 &lt; 冲突）。 */
export function highlightDiffCodeRaw(raw: string, lang: string, escapeHtml: (s: string) => string): string {
  if (!raw) return '';
  if (lang === 'text') return escapeHtml(raw);
  if (lang === 'html' || lang === 'xml') return highlightHtmlRaw(raw, escapeHtml);
  if (lang === 'css') return highlightCssRaw(raw, escapeHtml);
  if (lang === 'javascript' || lang === 'typescript') return highlightJsRaw(raw, escapeHtml);
  if (lang === 'python') return highlightPythonRaw(raw, escapeHtml);
  if (lang === 'json') return highlightJsonRaw(raw, escapeHtml);
  return escapeHtml(raw);
}

function isNewFileDiff(lines: DiffLine[]): boolean {
  return lines.length > 0 && lines.every((line) => line.kind === 'add');
}

export interface RenderDiffHtmlOptions {
  escapeHtml: (value: string) => string;
  /** 用于推断语法高亮语言 */
  filename?: string;
  /** 默认 8；设为 0 禁用折叠。 */
  collapseThreshold?: number;
  /** 各折叠区已揭开行数（按 region start）。 */
  expands?: DiffRegionExpandMap;
}

const DIFF_SKIP_CHEV_DOWN = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M6 9l6 6 6-6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
const DIFF_SKIP_CHEV_UP = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M18 15l-6-6-6 6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>`;

/**
 * 生成 Inspector 文件卡片内的 diff HTML（Cursor / Codex 风格）。
 * 单行 = 行号 + +/- 标记 + 代码；长段未改动折叠为可上下展开的 unmodified 条。
 */
export function renderDiffPanelHtml(
  rows: BackendDiffRow[],
  options: RenderDiffHtmlOptions,
): string {
  const {
    escapeHtml,
    filename,
    collapseThreshold = CONTEXT_COLLAPSE_THRESHOLD,
    expands = {},
  } = options;
  const parsed = parseBackendDiffRows(rows);
  if (parsed.length === 0) {
    return '<div class="inspector-file__diff-empty">暂无 diff 内容</div>';
  }

  const lang = languageFromFilename(filename);
  const newFile = isNewFileDiff(parsed);
  const display = collapseThreshold > 0
    ? collapseContextRuns(parsed, collapseThreshold, expands)
    : parsed.map((line) => ({ type: 'line' as const, line }));

  const rowHtml: string[] = [];

  for (const item of display) {
    if (item.type === 'collapsed') {
      const label = `${item.count.toLocaleString('en-US')} unmodified lines`;
      rowHtml.push(
        `<div class="inspector-file__diff-row inspector-file__diff-row--skip" data-diff-region-start="${item.start}" data-diff-region-end="${item.end}" data-diff-hidden="${item.count}">`
        + `<div class="inspector-file__diff-skip" role="group" aria-label="${escapeHtml(label)}">`
        + `<span class="inspector-file__diff-skip-controls">`
        + `<button type="button" class="inspector-file__diff-skip-btn" data-diff-expand="top" title="向下展开 ${CONTEXT_EXPAND_CHUNK} 行" aria-label="向下展开 ${CONTEXT_EXPAND_CHUNK} 行未改动内容">${DIFF_SKIP_CHEV_DOWN}</button>`
        + `<button type="button" class="inspector-file__diff-skip-btn" data-diff-expand="bottom" title="向上展开 ${CONTEXT_EXPAND_CHUNK} 行" aria-label="向上展开 ${CONTEXT_EXPAND_CHUNK} 行未改动内容">${DIFF_SKIP_CHEV_UP}</button>`
        + `</span>`
        + `<span class="inspector-file__diff-skip-label">${escapeHtml(label)}</span>`
        + `</div>`
        + `</div>`,
      );
      continue;
    }

    const { line } = item;
    const cls = kindClass(line.kind);
    const sign = diffSignForKind(line.kind);
    const signCls = line.kind === 'add'
      ? 'inspector-file__diff-sign--add'
      : line.kind === 'remove'
        ? 'inspector-file__diff-sign--del'
        : 'inspector-file__diff-sign--ctx';
    const raw = line.text || ' ';
    const codeHtml = raw.trim() && lang !== 'text'
      ? highlightDiffCodeRaw(raw, lang, escapeHtml)
      : escapeHtml(raw);

    rowHtml.push(
      `<div class="inspector-file__diff-row ${cls}">`
      + `<span class="inspector-file__diff-lno" aria-hidden="true">${escapeHtml(lineNoForGutter(line))}</span>`
      + `<span class="inspector-file__diff-sign ${signCls}" aria-hidden="true">${escapeHtml(sign)}</span>`
      + `<code class="inspector-file__diff-code" data-diff-lang="${escapeHtml(lang)}">${codeHtml}</code>`
      + `</div>`,
    );
  }

  const panelMods = [
    'inspector-file__diff-panel',
    newFile ? 'inspector-file__diff-panel--new-file' : '',
  ].filter(Boolean).join(' ');

  return `
    <div class="${panelMods}" data-slot="file-diff-panel" data-diff-lang="${escapeHtml(lang)}">
      <div class="inspector-file__diff-scroll">
        <div class="inspector-file__diff-scroll-inner">
          ${rowHtml.join('')}
        </div>
      </div>
    </div>
  `;
}

/** 在 DOM 挂载后补上色（渲染阶段已上色则跳过）。 */
export function applyDiffSyntaxHighlights(root: ParentNode, escapeHtmlFn: (s: string) => string): void {
  root.querySelectorAll<HTMLElement>('.inspector-file__diff-code[data-diff-lang]').forEach((el) => {
    if (el.querySelector('.diff-tok')) return;
    const lang = el.getAttribute('data-diff-lang') || 'text';
    const raw = el.textContent ?? '';
    if (!raw.trim()) return;
    el.innerHTML = highlightDiffCodeRaw(raw, lang, escapeHtmlFn);
  });
}
