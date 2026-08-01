/**
 * Composer 触发式补全：输入 `@` 补全项目文件，输入 `/` 补全技能。
 *
 * 取代旧的 complete-popup（只支持 @、贴输入框角、无防抖、邮箱 foo@bar 误触发）。
 *
 * 设计要点：
 *  - **触发判定**按光标往前扫到边界：`@`/`/` 必须在行首或空格之后才触发，
 *    杜绝邮箱 / `a/b` 这类误触发；`/` 只接 slug 字符，挡掉 `//`、`/*`。
 *  - **浮层锚到触发符的像素坐标**（textarea-caret 镜像量算），而不是输入框角落。
 *  - **数据源分流**：`@` 走后端 `/api/complete`（120ms 防抖）；`/` 走本地 skillsCache 过滤（即时）。
 *  - **回填纯文本**：`@` 插 `@file:src/a.ts`；`/` 插 `/中文名`（后端 resolve_skill 已支持按
 *    display_name 解析），无中文名则回退 `/slug`。textarea 仍是 input.value 真相源。
 *  - **chip = 透明 textarea + 全量覆盖层**：textarea 文字透明（仅 caret 可见，IME 合成期由
 *    覆盖层镜像合成文本），覆盖层重绘整段正文——普通字正常色、`@file:`/`@folder:`/`@image:`
 *    与已知 `/中文名`/`/slug` 区间染蓝。因覆盖层与 textarea 同字体同换行（copyTextareaStyle），
 *    光标与文字像素对齐；合成期临时停画 chip，避免与 IME 预览冲突。
 *  - **Backspace** 在已成型 chip 末尾一次删整段（atomic）。
 *  - **发送守卫**：popup 打开时，index.ts 的 Enter→发送 必须让位（见 isMentionOpen）。
 */

import { backendApi, type CompleteItem, type Skill } from '../backend-client';
import { $, escapeHtml } from '../state';
import { composerWorkspaceId } from './workspaces';
import { copyTextareaStyle, getCaretCoords } from '../lib/textarea-caret';
import { pinyin as toPinyin } from 'pinyin-pro';

type Trigger = '@' | '/';

interface ActiveTrigger {
  trigger: Trigger;
  /** value 中触发符 `@`/`/` 的下标。 */
  start: number;
}

interface MentionItem {
  /** 回填文本（含前缀），如 `@file:src/a.ts` 或 `/skill-slug`。 */
  text: string;
  display: string;
  meta: string;
  /** 图标类型：slash / folder / image / file。 */
  sig: 'slash' | 'folder' | 'image' | 'file';
}

interface ChipToken {
  start: number;
  end: number;
  kind: 'at' | 'slash';
}

/** 技能的拼音索引：全拼（连写）+ 首字母串。用于「输入 wangye / wyss 搜到 网页搜索」。 */
export interface SkillPinyin {
  full: string;
  initials: string;
}

let bound = false;
let input: HTMLTextAreaElement | null = null;
let overlay: HTMLElement | null = null;
let popup: HTMLElement | null = null;
let items: MentionItem[] = [];
let active: ActiveTrigger | null = null;
let selectedIndex = 0;

let skillsCache: Skill[] | null = null;
let skillsCachePromise: Promise<Skill[]> | null = null;
/** slug → 拼音索引，skills 载入时预计算。 */
const pinyinCache = new Map<string, SkillPinyin>();

/** 丢弃过期请求：每次发起新查询自增，回来时比对。 */
let fetchSeq = 0;
let debounceTimer: number | null = null;

/** IME 合成态：textarea 文字透明，合成中的中文需由覆盖层镜像显示，否则用户看不见输入。 */
let composing = false;
let composingText = '';

/** popup 打开且有候选项时为 true——index.ts 的发送守卫据此让 Enter 走「选中」。 */
export function isMentionOpen(): boolean {
  return popup != null && items.length > 0;
}

// ---------------- 触发判定 ----------------

/** 从光标往前找触发符；命中返回 {trigger,start}，否则 null。纯函数，可单测。 */
export function detectTrigger(value: string, caret: number): ActiveTrigger | null {
  if (caret <= 0) return null;
  // 往前扫一段连续「非空白」字符
  let i = caret;
  while (i > 0 && !/\s/.test(value[i - 1]!)) i -= 1;
  const run = value.slice(i, caret);
  if (!run) return null;
  const triggerChar = run[0];
  if (triggerChar !== '@' && triggerChar !== '/') return null;
  // 触发符前必须是行首或空白（边界），否则 foo@bar / path/to 会误触发
  const before = i > 0 ? value[i - 1]! : '';
  if (before && !/\s/.test(before)) return null;
  const query = run.slice(1);
  // `/` 允许任意非空白查询（含中文，用于按中文名搜技能）；只挡 `//`、`/*` 这类。
  // `a/b`、`https://` 已由上面的「边界」判定排除（/ 前非空白即不触发）。
  if (triggerChar === '/' && query.length > 0 && (query[0] === '/' || query[0] === '*')) return null;
  return { trigger: triggerChar as Trigger, start: i };
}

// ---------------- 数据源 ----------------

/** 计算一段文本的拼音索引（全拼连写 + 首字母）。非中文字符 pinyin-pro 原样保留。 */
export function computePinyin(text: string): SkillPinyin {
  if (!text) return { full: '', initials: '' };
  try {
    const syllables = toPinyin(text, { toneType: 'none', type: 'array' }) as unknown as string[];
    const firstLetters = toPinyin(text, { pattern: 'first', toneType: 'none', type: 'array' }) as unknown as string[];
    return { full: syllables.join('').toLowerCase(), initials: firstLetters.join('').toLowerCase() };
  } catch {
    return { full: '', initials: '' };
  }
}

/** 取一个技能的拼音索引。只对中文名计算——拉丁 slug/name 的匹配由字符层负责，
 *  且 pinyin-pro 会把拉丁串拆成单字母污染首字母索引。 */
function pinyinOf(s: Skill): SkillPinyin {
  const cached = pinyinCache.get(s.slug);
  if (cached) return cached;
  const py = computePinyin(s.display_name || '');
  pinyinCache.set(s.slug, py);
  return py;
}

async function ensureSkills(): Promise<Skill[]> {
  if (skillsCache) return skillsCache;
  if (!skillsCachePromise) {
    skillsCachePromise = backendApi
      .skills()
      .then((list) => {
        skillsCache = list;
        // 预计算拼音索引，让首字母/全拼搜索零延迟
        pinyinCache.clear();
        for (const s of list) pinyinOf(s);
        return list;
      })
      .catch(() => {
        // 失败不缓存：空数组是 truthy，会被 `if (skillsCache) return` 永久当成「已加载」，
        // 导致首启未连后端时 / 永远没反应。清掉 promise 让下次重试，本次返回空。
        skillsCachePromise = null;
        return [] as Skill[];
      });
  }
  return skillsCachePromise;
}

/** 启动时预取 skills：未连后端会失败，按秒退避重试，直到拿到（让首条 / 零延迟）。 */
let prefetchTries = 0;
function prefetchSkills(): void {
  if (skillsCache) return;
  void ensureSkills().then(() => {
    renderOverlay();
    if (!skillsCache && prefetchTries < 8) {
      prefetchTries += 1;
      window.setTimeout(prefetchSkills, 1000);
    }
  });
}

// ---------------- 模糊匹配（slug / name / display_name / description 多维 + 容错） ----------------
//
// 分层打分，越大越优先。纯函数，可单测。
//   6  slug 或中文名 整词相等
//   5  slug / 中文名 / name 前缀命中
//   4  slug / 中文名 / name 子串命中
//   3  description 子串命中
//   2  slug / 中文名 / name 子序列命中（缩写：wbs → web-search）
//   1  slug / 中文名 滑窗编辑距离 ≤1（打错一个字：网页索 → 网页搜索）
//   0  不匹配
// 空查询：所有技能同分（保留原序），让用户看到全部可选。

function isSubsequence(needle: string, hay: string): boolean {
  if (!needle) return true;
  let i = 0;
  for (let j = 0; j < hay.length && i < needle.length; j += 1) {
    if (hay[j] === needle[i]) i += 1;
  }
  return i === needle.length;
}

/** 编辑距离是否 ≤ k（早退：一旦超过 k 即返回 false）。字符串都很短，朴素 DP 足够。 */
function editLeqK(a: string, b: string, k: number): boolean {
  const la = a.length;
  const lb = b.length;
  if (Math.abs(la - lb) > k) return false;
  let prev = Array.from({ length: lb + 1 }, (_, j) => j);
  for (let i = 1; i <= la; i += 1) {
    const cur = [i];
    let rowMin = cur[0]!;
    for (let j = 1; j <= lb; j += 1) {
      const cost = a[i - 1] === b[j - 1] ? 0 : 1;
      cur[j] = Math.min(prev[j]! + 1, cur[j - 1]! + 1, prev[j - 1]! + cost);
      if (cur[j]! < rowMin) rowMin = cur[j]!;
    }
    if (rowMin > k) return false;
    prev = cur;
  }
  return prev[lb]! <= k;
}

/** field 中是否存在某段与 q 的编辑距离 ≤ k（滑窗，用于「打错一个字」容错）。 */
function windowEditWithinK(field: string, q: string, k: number): boolean {
  if (!q) return true;
  const ql = q.length;
  for (let w = Math.max(1, ql - k); w <= ql + k; w += 1) {
    for (let start = 0; start + w <= field.length; start += 1) {
      if (editLeqK(q, field.slice(start, start + w), k)) return true;
    }
  }
  return false;
}

/** 给一个技能对查询 q 打分（0 = 不匹配）。纯函数，可单测。py 为可选拼音索引。 */
export function matchSkill(s: Skill, q: string, py?: SkillPinyin | null): number {
  const query = q.trim().toLowerCase();
  if (!query) return 99;
  const slug = s.slug.toLowerCase();
  const name = (s.name || '').toLowerCase();
  const disp = (s.display_name || '').toLowerCase();
  const desc = (s.description_zh || s.description || '').toLowerCase();
  if (slug === query || disp === query) return 6;
  if (slug.startsWith(query) || disp.startsWith(query) || name.startsWith(query)) return 5;
  if (slug.includes(query) || disp.includes(query) || name.includes(query)) return 4;
  if (desc.includes(query)) return 3;
  // 拼音：全拼子串（wangye）/ 首字母前缀或整词（wys）—— 仅对拉丁查询有意义
  if (py && /[a-z0-9]/.test(query) && (py.full.includes(query) || py.initials === query || py.initials.startsWith(query))) return 3;
  // 宽松层（子序列 / 错字）只在查询 ≥3 字符时启用：2 字查询（如「网页」）靠精确层即可，
  // 否则子序列/编辑距离会把含「博」或「查」的无关技能也带进来，拉低精度。
  if (query.length >= 3) {
    if (isSubsequence(query, slug) || isSubsequence(query, disp) || isSubsequence(query, name)) return 2;
    if (py && isSubsequence(query, py.initials)) return 2;
    if (windowEditWithinK(slug, query, 1) || windowEditWithinK(disp, query, 1)) return 1;
  }
  return 0;
}

function filterSkills(query: string): MentionItem[] {
  const q = query.trim().toLowerCase();
  const list = skillsCache ?? [];
  return list
    .map((s) => {
      const disp = (s.display_name || s.name || s.slug).toLowerCase();
      return {
        s,
        score: matchSkill(s, q, pinyinOf(s)),
        prefix: disp.startsWith(q),
        dispLen: disp.length,
      };
    })
    .filter((x) => x.score > 0)
    .sort((a, b) => {
      // 主排序：分数 desc
      if (b.score !== a.score) return b.score - a.score;
      // 同分：前缀命中（更精确）优先
      const ap = a.prefix ? 1 : 0;
      const bp = b.prefix ? 1 : 0;
      if (ap !== bp) return bp - ap;
      // 同分：展示名更短（更像整词命中）优先
      return a.dispLen - b.dispLen;
    })
    .slice(0, 30)
    .map((x) => ({
      text: mentionTextForSkill(x.s, list),
      display: x.s.display_name || x.s.name || x.s.slug,
      meta: x.s.description_zh || x.s.description || '',
      sig: 'slash' as const,
    }));
}

/** 插入技能时优先用唯一、无空白的 display_name；否则回退稳定 slug。 */
export function mentionTextForSkill(skill: Skill, allSkills: Skill[] = skillsCache ?? []): string {
  const display = (skill.display_name || '').trim();
  if (!display || /\s/.test(display)) return `/${skill.slug}`;

  const norm = display.toLowerCase();
  const matchingSlugs = new Set<string>();
  for (const item of allSkills) {
    const itemDisplay = (item.display_name || '').trim().toLowerCase();
    const itemName = (item.name || '').trim().toLowerCase();
    if (itemDisplay === norm || itemName === norm) matchingSlugs.add(item.slug);
  }
  return matchingSlugs.size === 1 && matchingSlugs.has(skill.slug) ? `/${display}` : `/${skill.slug}`;
}

/** @ 补全用的工作空间：复用工作区模块的单一判定，默认对话只走 default task workspace。 */
function activeWorkspaceId(): string {
  return composerWorkspaceId();
}

async function fetchFileItems(token: string): Promise<MentionItem[]> {
  const rows: CompleteItem[] = await backendApi.complete(token, {
    workspaceId: activeWorkspaceId(),
  });
  return rows.map((r) => ({
    text: r.text,
    display: r.display,
    meta: r.meta,
    sig: r.type === 'folder' ? 'folder' : r.type === 'image' ? 'image' : 'file',
  }));
}

// ---------------- chip token 识别（覆盖层 + 整段删共用） ----------------

// 已解析的 @ 提及：必须是 @file:/@folder:/@image: 前缀（complete_path 的回填格式）
const AT_RE = /(?:^|\s)(@(?:file|folder|image):[^\s@]+)/g;

/** 当前已知技能的 chip 文本集合：/中文名 与 /slug（用于覆盖层染色 + 整段删识别）。 */
function currentSlashTokens(): Set<string> {
  const set = new Set<string>();
  const list = skillsCache ?? [];
  for (const s of list) {
    set.add(`/${s.slug}`);
    set.add(mentionTextForSkill(s, list));
  }
  return set;
}

function isTokenBoundary(value: string, idx: number): boolean {
  const ch = value[idx];
  return ch == null || /\s/.test(ch);
}

function normalizeChipTokens(tokens: ChipToken[]): ChipToken[] {
  const ordered = [...tokens].sort((a, b) => {
    if (a.start !== b.start) return a.start - b.start;
    return (b.end - b.start) - (a.end - a.start);
  });
  const out: ChipToken[] = [];
  let cursor = -1;
  for (const token of ordered) {
    if (token.start < cursor) continue;
    out.push(token);
    cursor = token.end;
  }
  return out;
}

/**
 * 枚举所有「已成型」的 chip 区间。/ chip 需命中已知技能（/中文名 或 /slug）；
 * @ chip 需带 file/folder/image 前缀。纯函数：slashTokens 缺省取本地缓存，测试可注入。
 */
export function iterChipTokens(value: string, slashTokens?: Set<string>): ChipToken[] {
  const out: ChipToken[] = [];
  const tokens = slashTokens ?? currentSlashTokens();

  AT_RE.lastIndex = 0;
  let m: RegExpExecArray | null;
  while ((m = AT_RE.exec(value))) {
    const lead = m[0].length - m[1]!.length; // 前导边界长度
    const start = m.index + lead;
    out.push({ start, end: start + m[1]!.length, kind: 'at' });
  }
  // / chip：对每个已知 token 做边界感知的子串扫描（token 含中文，无法用单一 ASCII 正则）
  for (const tok of tokens) {
    let from = 0;
    while (from <= value.length) {
      const idx = value.indexOf(tok, from);
      if (idx < 0) break;
      const end = idx + tok.length;
      if (isTokenBoundary(value, idx - 1) && isTokenBoundary(value, end)) out.push({ start: idx, end, kind: 'slash' });
      from = idx + tok.length;
    }
  }
  return normalizeChipTokens(out);
}

/**
 * 把一个 chip token 拆成「标记 + 可见名」：标记（/ @file: @folder: @image:）渲染成透明
 * （不可见但占宽，保证光标与存储文本对齐、后端 dispatch 仍拿到完整 token），可见名染蓝。
 * 纯函数，可单测。
 */
export function renderChip(token: string, kind: 'at' | 'slash'): { mark: string; body: string } {
  if (kind === 'slash') return { mark: '/', body: token.slice(1) };
  const m = token.match(/^@(file|folder|image):/);
  if (m) return { mark: m[0], body: token.slice(m[0].length) };
  return { mark: '', body: token };
}

/**
 * 把含 chip 的纯文本渲染成 DOM 节点序列（普通文本节点 + 蓝色 chip span）。
 * 输入框覆盖层与消息气泡共用此函数，避免两处各写一遍 chip 构造。
 * 用 createTextNode / textContent，不经过 innerHTML，XSS 安全。
 * slashTokens 缺省取本地 skills 缓存（气泡渲染时若未载入，/中文名 暂不染色，@file: 不受影响）。
 */
export function buildChippedNodes(text: string, slashTokens?: Set<string>): Node[] {
  const nodes: Node[] = [];
  const tokens = iterChipTokens(text, slashTokens);
  let cursor = 0;
  for (const t of tokens) {
    if (t.start < cursor) continue; // 重叠保护
    if (t.start > cursor) nodes.push(document.createTextNode(text.slice(cursor, t.start)));
    const { mark, body } = renderChip(text.slice(t.start, t.end), t.kind);
    const chip = document.createElement('span');
    chip.className = `mention-chip mention-chip--${t.kind}`;
    if (mark) {
      const markSpan = document.createElement('span');
      markSpan.className = 'mention-chip__mark';
      markSpan.textContent = mark; // 透明占位
      chip.appendChild(markSpan);
    }
    chip.appendChild(document.createTextNode(body)); // 染蓝的可见名
    nodes.push(chip);
    cursor = t.end;
  }
  if (cursor < text.length) nodes.push(document.createTextNode(text.slice(cursor)));
  return nodes;
}

// ---------------- 浮层渲染 ----------------

function sigMarkup(sig: MentionItem['sig']): string {
  const map: Record<MentionItem['sig'], string> = {
    slash: '<span class="mention-pop__sig mention-pop__sig--slash">/</span>',
    folder: '<span class="mention-pop__sig">📁</span>',
    image: '<span class="mention-pop__sig">🖼️</span>',
    file: '<span class="mention-pop__sig">📄</span>',
  };
  return map[sig];
}

function renderPopup(): void {
  if (!input || !active) return;
  const coords = getCaretCoords(input, active.start);
  if (!coords) return;

  popup?.remove();
  popup = document.createElement('div');
  popup.id = 'mention-popup';
  popup.className = 'mention-pop';
  popup.setAttribute('role', 'listbox');
  popup.innerHTML = items
    .map(
      (item, i) => `
      <button type="button" class="mention-pop__item${i === selectedIndex ? ' is-active' : ''}" role="option" data-idx="${i}">
        ${sigMarkup(item.sig)}
        <span class="mention-pop__body">
          <span class="mention-pop__display">${escapeHtml(item.display)}</span>
          ${item.meta ? `<span class="mention-pop__meta">${escapeHtml(item.meta)}</span>` : ''}
        </span>
      </button>`,
    )
    .join('');

  // 锚到 .chat-input-row（CSS 已置 position:relative），bottom:100% 贴在输入行上方
  const host = input.parentElement;
  if (host) {
    host.appendChild(popup);
    popup.style.left = `${Math.max(0, coords.left)}px`;
  }
  popup.querySelectorAll<HTMLElement>('.mention-pop__item').forEach((btn) => {
    btn.addEventListener('mousedown', (e) => {
      e.preventDefault(); // 不让 textarea 失焦
      const idx = Number(btn.getAttribute('data-idx'));
      selectedIndex = idx;
      pickSelected();
    });
  });
}

function markActive(): void {
  if (!popup) return;
  popup.querySelectorAll<HTMLElement>('.mention-pop__item').forEach((el, i) => {
    el.classList.toggle('is-active', i === selectedIndex);
  });
}

function closePopup(): void {
  popup?.remove();
  popup = null;
  active = null;
  items = [];
  selectedIndex = 0;
  if (debounceTimer != null) {
    window.clearTimeout(debounceTimer);
    debounceTimer = null;
  }
}

/** popup 打开时若 textarea 滚动/缩放，重新量算触发符横坐标。 */
function repositionPopup(): void {
  if (!popup || !input || !active) return;
  const coords = getCaretCoords(input, active.start);
  if (coords) popup.style.left = `${Math.max(0, coords.left)}px`;
}

// ---------------- 查询调度 ----------------

function schedule(trigger: Trigger, token: string): void {
  if (debounceTimer != null) window.clearTimeout(debounceTimer);
  const scheduledSeq = ++fetchSeq;
  // @ 要打网络，防抖 120ms；/ 本地过滤，给一帧（40ms）避免连击抖动
  const delay = trigger === '@' ? 120 : 40;
  debounceTimer = window.setTimeout(() => {
    debounceTimer = null;
    void run(trigger, token, scheduledSeq);
  }, delay);
}

async function run(trigger: Trigger, token: string, mySeq: number): Promise<void> {
  let result: MentionItem[];
  if (trigger === '@') {
    result = await fetchFileItems(token).catch(() => []);
  } else {
    await ensureSkills();
    result = filterSkills(token.slice(1));
  }
  // 过期：用户继续输入发起了更新的查询
  if (mySeq !== fetchSeq) return;
  if (!input) return;
  // 二次确认光标仍在同一触发符上（fetch 期间用户可能已删/移走）
  const recheck = detectTrigger(input.value, input.selectionStart ?? 0);
  const caret = input.selectionStart ?? 0;
  if (!recheck || recheck.trigger !== trigger || recheck.start !== active?.start || input.value.slice(recheck.start, caret) !== token) {
    closePopup();
    return;
  }
  items = result;
  selectedIndex = 0;
  if (items.length === 0) {
    closePopup();
    return;
  }
  renderPopup();
}

// ---------------- 选中回填 ----------------

function pickSelected(): void {
  if (!input || !active) return;
  const item = items[selectedIndex];
  if (!item) return;
  const start = active.start;
  const value = input.value;
  const caret = input.selectionStart ?? value.length;
  const next = value.slice(0, start) + item.text + ' ' + value.slice(caret);
  input.value = next;
  const newCaret = start + item.text.length + 1;
  input.setSelectionRange(newCaret, newCaret);
  input.dispatchEvent(new Event('input', { bubbles: true }));
  input.focus();
  closePopup();
  renderOverlay();
}

// ---------------- 整段删 chip ----------------

function maybeBackspaceChip(e: KeyboardEvent): void {
  if (!input) return;
  // popup 打开时不抢 Backspace（让用户删查询字符自然关闭浮层）
  if (popup) return;
  if (input.selectionStart !== input.selectionEnd) return;
  const value = input.value;
  const caret = input.selectionStart ?? value.length;
  if (caret <= 0) return;
  const tokens = iterChipTokens(value);
  // 命中：光标紧跟 token 末尾；或紧跟「token + 一个尾随空格」（pickSelected 会补尾随空格，
  // 此时光标在空格后——连空格一起整块删，避免先逐字删空格的「一点一点删」手感）。
  let start = -1;
  let end = -1;
  const direct = tokens.find((t) => t.end === caret);
  if (direct) {
    start = direct.start;
    end = direct.end;
  } else if (value[caret - 1] === ' ') {
    const withSpace = tokens.find((t) => t.end === caret - 1);
    if (withSpace) {
      start = withSpace.start;
      end = caret; // 含尾随空格
    }
  }
  if (start < 0) return;
  e.preventDefault();
  input.value = value.slice(0, start) + value.slice(end);
  input.setSelectionRange(start, start);
  input.dispatchEvent(new Event('input', { bubbles: true }));
  renderOverlay();
}

// ---------------- chip 覆盖层 ----------------

function ensureOverlay(): void {
  if (!input) return;
  const host = input.parentElement;
  if (!host) return;
  host.style.position = 'relative';
  if (!overlay) {
    overlay = document.createElement('div');
    overlay.className = 'chat-input-overlay';
    overlay.setAttribute('aria-hidden', 'true');
    host.insertBefore(overlay, input);
  }
  // 每次同步字体/盒模型（主题/字号可能变化）
  copyTextareaStyle(input, overlay);
  // 强制 textarea 透明（内联样式最高优先级）：避免 CSS 缓存/加载时序导致 textarea 文字
  // 与覆盖层同时可见，表现为「文字双倍显示」。caretColor 保留可见光标。
  input.style.color = 'transparent';
  input.style.caretColor = 'var(--tx)';
}

function renderOverlay(): void {
  if (!input || !overlay) return;
  overlay.style.width = `${input.clientWidth}px`;
  overlay.style.height = `${input.clientHeight}px`;
  const inComposing = composing && composingText.length > 0;
  // Windows 输入法会在 textarea 上独立绘制合成串，即使 textarea 文本透明仍可见。
  // overlay 再插入 composingText 会把拼音显示两遍；合成期只停画 chip，展示已提交文本。
  const nodes = inComposing ? [document.createTextNode(input.value)] : buildChippedNodes(input.value);
  overlay.replaceChildren(...nodes);
  overlay.scrollTop = input.scrollTop;
}

// ---------------- 事件处理 ----------------

/** 重新评估光标处是否处于触发位：是则弹补全，否则关闭。
 *  click / 方向键移动光标后也调用——否则用户补空格后把光标点到 /词 末尾不会触发，
 *  必须再敲一个字才行。 */
function evaluateTrigger(): void {
  if (composing || !input) return;
  const value = input.value;
  const caret = input.selectionStart ?? value.length;
  const trig = detectTrigger(value, caret);
  if (!trig) {
    closePopup();
    return;
  }
  active = trig;
  schedule(trig.trigger, value.slice(trig.start, caret));
}

function onInput(): void {
  renderOverlay();
  // IME 合成中不弹补全（合成文本还未落定，避免误判触发符）
  if (composing) return;
  evaluateTrigger();
}

function onKeydown(e: KeyboardEvent): void {
  if (popup && items.length > 0) {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      selectedIndex = (selectedIndex + 1) % items.length;
      markActive();
      return;
    }
    if (e.key === 'ArrowUp') {
      e.preventDefault();
      selectedIndex = (selectedIndex - 1 + items.length) % items.length;
      markActive();
      return;
    }
    // IME 合成中不抢 Enter（让选字回车通过）；Tab/Enter 选中
    if ((e.key === 'Enter' || e.key === 'Tab') && !e.shiftKey && !e.isComposing && e.keyCode !== 229) {
      e.preventDefault();
      pickSelected();
      return;
    }
    if (e.key === 'Escape') {
      e.preventDefault();
      closePopup();
      return;
    }
  }
  if (e.key === 'Backspace') {
    maybeBackspaceChip(e);
  }
}

/**
 * 绑定 #chat-input 的触发式补全。幂等。替代旧 bindCompletePopup。
 * 由 ui/index.ts 在 init 时调用一次。
 */
export function bindComposerMention(): void {
  if (bound) return;
  input = ($('#chat-input') as HTMLTextAreaElement | null) ?? null;
  if (!input) return;
  bound = true;

  ensureOverlay();
  renderOverlay();

  input.addEventListener('input', onInput);
  input.addEventListener('keydown', onKeydown);
  // 光标移动（点击定位 / 方向键）后重判触发：补空格后把光标置于 /词 末尾即可弹补全
  input.addEventListener('click', evaluateTrigger);
  input.addEventListener('focus', evaluateTrigger);
  input.addEventListener('keyup', (e) => {
    if (popup) return; // popup 打开时方向键走导航
    if (e.key === 'ArrowLeft' || e.key === 'ArrowRight' || e.key === 'ArrowUp' || e.key === 'ArrowDown' || e.key === 'Home' || e.key === 'End') {
      evaluateTrigger();
    }
  });
  // IME 合成：textarea 文字透明，合成中文需覆盖层镜像显示
  input.addEventListener('compositionstart', () => {
    composing = true;
    composingText = '';
  });
  input.addEventListener('compositionupdate', (e) => {
    composingText = (e as CompositionEvent).data ?? '';
    renderOverlay();
  });
  input.addEventListener('compositionend', () => {
    composing = false;
    composingText = '';
    renderOverlay();
    // IME 提交后重判触发：否则中文输入（/网页、@文件 中文片段）提交后不刷新浮层——
    // 表现为「/网页无反应，删一个字才有」，因为删除是 Latin 按键走 input 事件，而输入是 IME。
    evaluateTrigger();
  });
  input.addEventListener('scroll', () => {
    if (overlay && input) overlay.scrollTop = input.scrollTop;
    repositionPopup();
  });
  input.addEventListener('blur', () => closePopup());
  window.addEventListener('resize', () => {
    ensureOverlay();
    renderOverlay();
    repositionPopup();
  });
  document.addEventListener('click', (e) => {
    const t = e.target as HTMLElement;
    if (popup && !popup.contains(t) && t !== input) closePopup();
  });

  // 预取 skills（首启未连后端会失败，prefetchSkills 按秒退避重试），让首条 / 零延迟；
  // 拿到后也补画一次 chip（/中文名 药丸才出现）。
  prefetchSkills();
}
