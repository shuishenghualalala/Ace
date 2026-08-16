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
 *  - **chip = 透明 textarea + 全量覆盖层**：textarea 文字透明（仅 caret 可见），覆盖层重绘
 *    整段正文——普通字正常色、`@file:`/`@folder:`/`@image:` 与已知 `/中文名`/`/slug` 区间染蓝。
 *    文件引用选中后使用短显示 token，发送时还原完整前缀。
 *    因覆盖层与 textarea 使用同一短 token（copyTextareaStyle），光标与文字像素对齐。
 *  - **IME 合成期交还原生绘制**：浏览器会把合成串画在 textarea 层（color:transparent 挡不住），
 *    且合成串会撑高/滚动 textarea，与只承载已提交文本的覆盖层必然错位叠字。因此 compositionstart
 *    时恢复 textarea 文字可见、隐藏覆盖层（原生排版天然对齐），compositionend 再切回覆盖层。
 *  - **Backspace** 在已成型 chip 末尾一次删整段（atomic）。
 *  - **发送守卫**：popup 打开时，index.ts 的 Enter→发送 必须让位（见 isMentionOpen）。
 */

import { backendApi, type BrowserPageState, type CompleteItem, type Skill, type WorkPreference } from '../backend-client';
import { createIcon, type IconId } from '../components/icon';
import { setRuntimeStyle, clearRuntimeStyle } from '../components/runtime-style';
import { $, state } from '../state';
import { productModeStore } from '../stores/product-mode-store';
import { sessionStore } from '../stores/session-store';
import { workStore } from '../stores/work-store';
import { composerWorkspaceId } from './workspaces';
import { queryPrimaryComposer } from './composer-scope';
import {
  MENTION_KINDS,
  removeMentionTag,
  renderMentionTags,
  searchMentions,
  selectMention,
  type MentionResult,
  type MentionTag,
} from './work/mentions';
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
  /** 图标类型：slash / folder / image / file / tab（浏览器标签页）。 */
  sig: 'slash' | 'folder' | 'image' | 'file' | 'tab';
  /** 弹窗候选左侧图标。 */
  icon: IconId;
  workResult?: MentionResult;
}

interface CompactMention {
  visible: string;
  canonical: string;
  kind: 'folder' | 'image' | 'file';
}

interface ChipToken {
  start: number;
  end: number;
  kind: 'at' | 'slash';
}

/** 技能的拼音索引：全拼（连写）+ 首字母串。用于「输入 bocha / bcss 搜到 博查搜索」。 */
export interface SkillPinyin {
  full: string;
  initials: string;
}

let bound = false;
let bindingController: AbortController | null = null;
let input: HTMLTextAreaElement | null = null;
let overlay: HTMLElement | null = null;
let popup: HTMLElement | null = null;
let items: MentionItem[] = [];
let active: ActiveTrigger | null = null;
let selectedIndex = 0;
let workTags: MentionTag[] = [];
let workTagsHost: HTMLElement | null = null;
/** 选中后的短显示 token，发送时还原成后端识别的结构化 token。 */
let compactMentions: CompactMention[] = [];
const disabledWorkPreferenceIds = new Set<string>();

let skillsCache: Skill[] | null = null;
let skillsCachePromise: Promise<Skill[]> | null = null;
/** slug → 拼音索引，skills 载入时预计算。 */
const pinyinCache = new Map<string, SkillPinyin>();

/** 丢弃过期请求：每次发起新查询自增，回来时比对。 */
let fetchSeq = 0;
let debounceTimer: number | null = null;

/** IME 合成态：合成期 textarea 恢复原生可见、覆盖层隐藏（见文件头「IME 合成期交还原生绘制」）。 */
let composing = false;

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
let prefetchTimer: number | null = null;
function prefetchSkills(): void {
  if (skillsCache) return;
  void ensureSkills().then(() => {
    renderOverlay();
    if (!skillsCache && prefetchTries < 8) {
      prefetchTries += 1;
      prefetchTimer = window.setTimeout(prefetchSkills, 1000);
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
//   2  slug / 中文名 / name 子序列命中（缩写：bch → bocha-search）
//   1  slug / 中文名 滑窗编辑距离 ≤1（打错一个字：博查索 → 博查搜索）
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
  // 拼音：全拼子串（bocha）/ 首字母前缀或整词（bcs）—— 仅对拉丁查询有意义
  if (py && /[a-z0-9]/.test(query) && (py.full.includes(query) || py.initials === query || py.initials.startsWith(query))) return 3;
  // 宽松层（子序列 / 错字）只在查询 ≥3 字符时启用：2 字查询（如「博查」）靠精确层即可，
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
      icon: 'skill-badge' as const,
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
  return productModeStore.get().productMode === 'work'
    ? workStore.get().selectedWorkspaceId ?? 'default'
    : composerWorkspaceId();
}

/** 标签页候选标题：页面标题优先，其次 URL，再次标签序号。 */
function browserTabTitle(tab: BrowserPageState['tabs'][number]): string {
  return tab.title.trim() || tab.url.trim() || tab.label.trim() || tab.id;
}

/** 按 query 过滤浏览器标签页并映射为提及候选（title/url 子串匹配）。纯函数，可单测。 */
export function filterBrowserTabs(tabs: BrowserPageState['tabs'], query: string): MentionResult[] {
  const q = query.trim().toLowerCase();
  return tabs
    .filter((tab) => !q || tab.title.toLowerCase().includes(q) || tab.url.toLowerCase().includes(q))
    .map((tab) => ({
      entity_type: 'browser_tab' as const,
      id: tab.id,
      title: browserTabTitle(tab),
      source_link: tab.url,
    }));
}

/**
 * 标签页列表按 sessionId 短 TTL 缓存：browserState 的返回与 query 无关（过滤在本地
 * filterBrowserTabs 做），120ms 防抖的每次击键都拉一次纯属浪费；进行中的请求去重，
 * 连发击键共享同一次拉取。TTL 内标签页开关最多 ~2s 后才反映到补全候选，可接受。
 */
const BROWSER_TABS_TTL_MS = 2000;
let browserTabsCache: { sessionId: string; fetchedAt: number; tabs: BrowserPageState['tabs'] } | null = null;
let browserTabsInflight: { sessionId: string; promise: Promise<BrowserPageState['tabs']> } | null = null;

function loadBrowserTabs(sessionId: string): Promise<BrowserPageState['tabs']> {
  const cached = browserTabsCache;
  if (cached && cached.sessionId === sessionId && Date.now() - cached.fetchedAt < BROWSER_TABS_TTL_MS) {
    return Promise.resolve(cached.tabs);
  }
  if (browserTabsInflight?.sessionId === sessionId) return browserTabsInflight.promise;
  const promise: Promise<BrowserPageState['tabs']> = backendApi
    .browserState(sessionId)
    .then((result) => {
      const tabs = result.state?.tabs ?? [];
      browserTabsCache = { sessionId, fetchedAt: Date.now(), tabs };
      return tabs;
    })
    .catch(() => {
      // 失败不写缓存（同 skills 的「失败不缓存」），避免一次抖动把补全锁空 2s
      return [] as BrowserPageState['tabs'];
    })
    .finally(() => {
      if (browserTabsInflight?.promise === promise) browserTabsInflight = null;
    });
  browserTabsInflight = { sessionId, promise };
  return promise;
}

/**
 * 浏览器标签页本地 provider（不走 /api/work/mentions）：取当前会话的标签页列表
 * （带短 TTL 缓存，见 loadBrowserTabs），击键只按 query 做本地过滤。
 * 无浏览器会话 / 接口未就绪 / 失败时静默返回空数组，不影响文件与 work 候选。
 */
export async function fetchBrowserTabMentions(query: string): Promise<MentionResult[]> {
  const sessionId = state.activeSessionId;
  if (!sessionId) return [];
  const tabs = await loadBrowserTabs(sessionId);
  return filterBrowserTabs(tabs, query);
}

/** 文件候选（/api/complete 来源）各 sig 的图标；work 提及类型的图标在 MENTION_KINDS 注册表。 */
const FILE_ROW_ICON: Record<'folder' | 'image' | 'file', IconId> = {
  folder: 'icon-folder',
  image: 'icon-image',
  file: 'icon-file',
};

async function fetchFileItems(token: string): Promise<MentionItem[]> {
  const rowsPromise = backendApi.complete(token, { workspaceId: activeWorkspaceId() });
  const workPromise = productModeStore.get().productMode === 'work'
    ? searchMentions(token.slice(1), activeWorkspaceId())
    : Promise.resolve([]);
  const browserPromise = fetchBrowserTabMentions(token.slice(1));
  const [rows, workResults, browserResults] = await Promise.all([rowsPromise, workPromise, browserPromise]);
  return (rows as CompleteItem[]).map<MentionItem>((r) => {
    const sig = r.type === 'folder' ? 'folder' as const : r.type === 'image' ? 'image' as const : 'file' as const;
    return { text: r.text, display: r.display, meta: r.meta, sig, icon: FILE_ROW_ICON[sig] };
  }).concat(workResults.concat(browserResults).map((result) => {
    const kind = MENTION_KINDS[result.entity_type];
    return {
      text: workMentionText(result),
      display: result.title,
      meta: kind.meta,
      sig: kind.sig,
      icon: kind.icon,
      workResult: result,
    };
  }));
}

export function compactMentionText(item: MentionItem): string {
  if (!['folder', 'image', 'file'].includes(item.sig)) return item.text;
  const prefix = `@${item.sig}:`;
  if (!item.text.startsWith(prefix)) return item.text;
  const visible = `@${item.text.slice(prefix.length)}`;
  if (!compactMentions.some((mention) => mention.visible === visible && mention.canonical === item.text)) {
    compactMentions.push({ visible, canonical: item.text, kind: item.sig as CompactMention['kind'] });
  }
  return visible;
}

function hasMentionToken(value: string, token: string): boolean {
  let from = 0;
  while (from <= value.length) {
    const index = value.indexOf(token, from);
    if (index < 0) return false;
    const before = index === 0 ? '' : value[index - 1]!;
    const after = value[index + token.length] ?? '';
    if ((!before || /\s/.test(before)) && (!after || /\s/.test(after))) return true;
    from = index + token.length;
  }
  return false;
}

function syncCompactMentions(value: string): void {
  compactMentions = compactMentions.filter((mention) => hasMentionToken(value, mention.visible));
}

/** 将输入框里的短显示 token 还原为 Gateway 识别的 @file/@folder/@image token。 */
export function serializeMentionInput(value: string): string {
  syncCompactMentions(value);
  let result = value;
  for (const mention of [...compactMentions].sort((a, b) => b.visible.length - a.visible.length)) {
    const escaped = mention.visible.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    result = result.replace(new RegExp(`(^|\\s)${escaped}(?=\\s|$)`, 'g'), (_match, lead: string) => `${lead}${mention.canonical}`);
  }
  return result;
}

/** Return preferences that can be known to apply before a Work turn is sent. */
export function applicableWorkPreferences(
  preferences: WorkPreference[],
  workspaceId: string,
): WorkPreference[] {
  return preferences.filter((preference) =>
    preference.status === 'active'
    && (
      preference.scope === 'global'
      || (preference.scope === 'workspace' && preference.scope_id === workspaceId)
    ));
}

/** Toggle one preference for the next Work turn only. */
export function disableWorkPreferenceForTurn(preferenceId: string, disabled = true): void {
  const id = preferenceId.trim();
  if (!id) return;
  if (disabled) disabledWorkPreferenceIds.add(id);
  else disabledWorkPreferenceIds.delete(id);
  renderWorkTags();
}

/** Replace the one-turn preference exclusions, for example when editing a queued message. */
export function setDisabledWorkPreferenceIdsForTurn(preferenceIds: readonly string[]): void {
  disabledWorkPreferenceIds.clear();
  for (const preferenceId of preferenceIds) {
    const id = preferenceId.trim();
    if (id) disabledWorkPreferenceIds.add(id);
  }
  renderWorkTags();
}

/** Consume and clear the preferences disabled for the next Work turn. */
export function takeDisabledWorkPreferenceIds(): string[] {
  const ids = [...disabledWorkPreferenceIds];
  disabledWorkPreferenceIds.clear();
  renderWorkTags();
  return ids;
}

export function workMentionText(result: MentionResult): string {
  return `@${result.entity_type}:${result.id}`;
}

// ---------------- chip token 识别（覆盖层 + 整段删共用） ----------------

// 已解析的 @ 提及：必须是 @file:/@folder:/@image: 或注册表里的 work 提及前缀
// （@work_item:/@browser_tab: 等）。新增提及类型只需在 MENTION_KINDS 加一行，此处随之派生。
const AT_KINDS = ['file', 'folder', 'image', ...Object.keys(MENTION_KINDS)];
const AT_RE = new RegExp(`(?:^|\\s)(@(?:${AT_KINDS.join('|')}):[^\\s@]+)`, 'g');

function compactCanonicalMentionsInInput(): void {
  if (!input) return;
  const value = input.value;
  const caret = input.selectionStart ?? value.length;
  const replacements: Array<{ start: number; end: number; visible: string }> = [];
  AT_RE.lastIndex = 0;
  let match: RegExpExecArray | null;
  while ((match = AT_RE.exec(value))) {
    const canonical = match[1]!;
    const kind = canonical.match(/^@(file|folder|image):/)?.[1] as CompactMention['kind'] | undefined;
    if (!kind) continue;
    const lead = match[0].length - canonical.length;
    const start = match.index + lead;
    const visible = `@${canonical.slice(kind.length + 2)}`;
    compactMentions.push({ visible, canonical, kind });
    replacements.push({ start, end: start + canonical.length, visible });
  }
  if (replacements.length === 0) return;

  let next = value;
  let nextCaret = caret;
  for (const replacement of replacements.reverse()) {
    next = next.slice(0, replacement.start) + replacement.visible + next.slice(replacement.end);
    if (caret >= replacement.end) nextCaret -= replacement.end - replacement.start - replacement.visible.length;
    else if (caret > replacement.start) nextCaret = replacement.start + replacement.visible.length;
  }
  input.value = next;
  input.setSelectionRange(nextCaret, nextCaret);
}

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
  for (const mention of compactMentions) {
    let from = 0;
    while (from <= value.length) {
      const idx = value.indexOf(mention.visible, from);
      if (idx < 0) break;
      const end = idx + mention.visible.length;
      if (isTokenBoundary(value, idx - 1) && isTokenBoundary(value, end)) {
        out.push({ start: idx, end, kind: 'at' });
      }
      from = end;
    }
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
 * 把一个 chip token 拆成「标记 + 可见名」：标记（/ @file: @folder: @image:）供覆盖层
 * 渲染成紧凑图标，可见名染蓝；选中的文件引用在 textarea 中使用短显示 token。
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
    const rawToken = text.slice(t.start, t.end);
    const compact = t.kind === 'at' ? compactMentions.find((mention) => mention.visible === rawToken) : undefined;
    const { mark, body } = compact
      ? { mark: `@${compact.kind}:`, body: rawToken.slice(1) }
      : renderChip(rawToken, t.kind);
    const chip = document.createElement('span');
    chip.className = `mention-chip mention-chip--${t.kind}`;
    if (mark) {
      const markSpan = document.createElement('span');
      const atKind = mark.match(/^@(file|folder|image):/)?.[1] as 'file' | 'folder' | 'image' | undefined;
      markSpan.className = `mention-chip__mark${atKind ? ` mention-chip__mark--${atKind}` : ''}`;
      markSpan.setAttribute('aria-hidden', 'true');
      if (atKind) {
        const iconByKind: Record<typeof atKind, IconId> = {
          file: 'icon-file',
          folder: 'icon-folder',
          image: 'icon-image',
        };
        markSpan.append(createIcon(iconByKind[atKind], { size: 16 }));
      } else {
        markSpan.textContent = mark;
      }
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

function createSig(item: MentionItem): HTMLElement {
  const element = document.createElement('span');
  element.className = `mention-pop__sig mention-pop__sig--${item.sig}`;
  element.append(createIcon(item.icon, { size: 20 }));
  return element;
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
  items.forEach((item, index) => {
    const button = document.createElement('button');
    const body = document.createElement('span');
    const display = document.createElement('span');
    button.type = 'button';
    button.className = `mention-pop__item${index === selectedIndex ? ' is-active' : ''}`;
    button.dataset.idx = String(index);
    button.setAttribute('role', 'option');
    button.setAttribute('aria-selected', String(index === selectedIndex));
    body.className = 'mention-pop__body';
    display.className = 'mention-pop__display';
    display.textContent = item.display;
    body.append(display);
    if (item.meta) {
      const meta = document.createElement('span');
      meta.className = 'mention-pop__meta';
      meta.textContent = item.meta;
      body.append(meta);
    }
    button.append(createSig(item), body);
    button.addEventListener('mousedown', (event) => {
      event.preventDefault();
      selectedIndex = index;
      pickSelected();
    });
    popup?.append(button);
  });

  // 锚到 .chat-input-row（CSS 已置 position:relative），bottom:100% 贴在输入行上方
  const host = input.parentElement;
  if (host) {
    host.appendChild(popup);
    setRuntimeStyle(popup, 'left', `${Math.max(0, coords.left)}px`);
  }
}

function markActive(): void {
  if (!popup) return;
  popup.querySelectorAll<HTMLElement>('.mention-pop__item').forEach((el, i) => {
    el.classList.toggle('is-active', i === selectedIndex);
    el.setAttribute('aria-selected', String(i === selectedIndex));
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
  if (coords) setRuntimeStyle(popup, 'left', `${Math.max(0, coords.left)}px`);
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
  const visibleText = compactMentionText(item);
  const next = value.slice(0, start) + visibleText + ' ' + value.slice(caret);
  input.value = next;
  const newCaret = start + visibleText.length + 1;
  input.setSelectionRange(newCaret, newCaret);
  input.dispatchEvent(new Event('input', { bubbles: true }));
  input.focus();
  if (item.workResult && state.activeSessionId && productModeStore.get().productMode === 'work') {
    void selectMention(item.workResult, state.activeSessionId)
      .then((tag) => {
        workTags.push(tag);
        renderWorkTags();
      })
      .catch(() => {
        input?.setAttribute('aria-invalid', 'true');
      });
  }
  closePopup();
  renderOverlay();
}

function renderWorkTags(): void {
  const inputRow = input?.parentElement;
  const panel = inputRow?.parentElement;
  if (!inputRow || !panel || productModeStore.get().productMode !== 'work') {
    workTagsHost?.remove();
    workTagsHost = null;
    return;
  }
  const preferences = applicableWorkPreferences(
    workStore.get().preferences,
    workStore.get().selectedWorkspaceId ?? 'default',
  );
  if (preferences.length === 0 && workTags.length === 0) {
    workTagsHost?.remove();
    workTagsHost = null;
    return;
  }
  if (!workTagsHost) {
    workTagsHost = document.createElement('div');
    panel.insertBefore(workTagsHost, inputRow);
  }
  workTagsHost.className = 'mw-work-composer-context';
  workTagsHost.replaceChildren();

  if (preferences.length > 0) {
    const preferenceHost = document.createElement('div');
    preferenceHost.className = 'mw-work-composer-preferences';
    preferenceHost.setAttribute('aria-label', '本次应用的工作偏好');
    for (const preference of preferences) {
      const disabled = disabledWorkPreferenceIds.has(preference.preference_id);
      const toggle = document.createElement('button');
      toggle.type = 'button';
      toggle.className = 'mw-work-composer-preference';
      toggle.dataset.preferenceId = preference.preference_id;
      toggle.dataset.disabled = String(disabled);
      toggle.textContent = `${disabled ? '本次不应用' : '本次应用'}：${preference.content}`;
      toggle.title = disabled ? '恢复本轮应用' : '仅本轮取消应用';
      toggle.addEventListener('click', () => {
        disableWorkPreferenceForTurn(preference.preference_id, !disabled);
      });
      preferenceHost.append(toggle);
    }
    workTagsHost.append(preferenceHost);
  }

  if (workTags.length === 0) return;
  const mentionHost = document.createElement('div');
  workTagsHost.append(mentionHost);
  renderMentionTags(mentionHost, workTags, (tag) => {
    void removeMentionTag(tag).then(() => {
      workTags = workTags.filter((candidate) => candidate !== tag);
      const token = workMentionText(tag.result);
      if (input) {
        input.value = input.value.replace(token, '').replace(/ {2,}/g, ' ');
        input.dispatchEvent(new Event('input', { bubbles: true }));
      }
      renderWorkTags();
    });
  });
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
  host.classList.add('chat-input-overlay-host');
  input.classList.add('chat-input-overlay-source');
  if (!overlay) {
    overlay = document.createElement('div');
    overlay.className = 'chat-input-overlay';
    overlay.setAttribute('aria-hidden', 'true');
    host.insertBefore(overlay, input);
  }
  // 每次同步字体/盒模型（主题/字号可能变化）
  copyTextareaStyle(input, overlay);
}

function renderOverlay(): void {
  if (!input || !overlay) return;
  overlay.replaceChildren(...buildChippedNodes(input.value));
  overlay.scrollTop = input.scrollTop;
}

/**
 * IME 合成期切换：恢复 textarea 文字可见（颜色取计算后的 caret-color，兼容 studio 等皮肤）、
 * 隐藏覆盖层。合成串由浏览器画在 textarea 层且会撑高/滚动它，若仍用覆盖层显示已提交文本，
 * 两层文字必然错位叠字（长输入时尤为明显）。
 */
function enterNativeComposing(): void {
  if (!input || !overlay) return;
  setRuntimeStyle(input, 'color', getComputedStyle(input).caretColor);
  setRuntimeStyle(overlay, 'visibility', 'hidden');
}

/** 合成结束：恢复「透明 textarea + 覆盖层」并按已提交文本重绘。 */
function exitNativeComposing(): void {
  if (!input || !overlay) return;
  clearRuntimeStyle(input, 'color');
  clearRuntimeStyle(overlay, 'visibility');
  renderOverlay();
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
  input?.removeAttribute('aria-invalid');
  compactCanonicalMentionsInInput();
  if (!input?.value.trim()) {
    workTags = [];
    compactMentions = [];
    renderWorkTags();
  }
  if (input) syncCompactMentions(input.value);
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
 * 绑定主对话 Composer 输入框的触发式补全。幂等。替代旧 bindCompletePopup。
 * 由 ui/index.ts 在 init 时调用一次。
 */
export function bindComposerMention(): () => void {
  if (bound) return () => {};
  input = queryPrimaryComposer<HTMLTextAreaElement>('[data-composer-input]') ?? null;
  if (!input) return () => {};
  bound = true;
  bindingController = new AbortController();
  const signal = bindingController.signal;

  ensureOverlay();
  renderOverlay();
  renderWorkTags();

  const unsubscribeWork = workStore.subscribe((next, previous) => {
    if (
      next.preferences !== previous.preferences
      || next.selectedWorkspaceId !== previous.selectedWorkspaceId
    ) renderWorkTags();
  });
  const unsubscribeMode = productModeStore.subscribe((next, previous) => {
    if (next.productMode === previous.productMode) return;
    if (next.productMode !== 'work') disabledWorkPreferenceIds.clear();
    renderWorkTags();
  });
  const unsubscribeSession = sessionStore.subscribe((next, previous) => {
    if (next.activeSessionId === previous.activeSessionId) return;
    disabledWorkPreferenceIds.clear();
    renderWorkTags();
  });

  input.addEventListener('input', onInput, { signal });
  input.addEventListener('keydown', onKeydown, { signal });
  // 光标移动（点击定位 / 方向键）后重判触发：补空格后把光标置于 /词 末尾即可弹补全
  input.addEventListener('click', evaluateTrigger, { signal });
  input.addEventListener('focus', evaluateTrigger, { signal });
  input.addEventListener('keyup', (e) => {
    if (popup) return; // popup 打开时方向键走导航
    if (e.key === 'ArrowLeft' || e.key === 'ArrowRight' || e.key === 'ArrowUp' || e.key === 'ArrowDown' || e.key === 'Home' || e.key === 'End') {
      evaluateTrigger();
    }
  }, { signal });
  // IME 合成：合成期 textarea 原生可见、覆盖层隐藏，避免两层文字错位叠字
  input.addEventListener('compositionstart', () => {
    composing = true;
    enterNativeComposing();
  }, { signal });
  input.addEventListener('compositionend', () => {
    composing = false;
    exitNativeComposing();
    // IME 提交后重判触发：否则中文输入（/博查、@文件 中文片段）提交后不刷新浮层——
    // 表现为「/博查无反应，删一个字才有」，因为删除是 Latin 按键走 input 事件，而输入是 IME。
    evaluateTrigger();
  }, { signal });
  input.addEventListener('scroll', () => {
    if (overlay && input) overlay.scrollTop = input.scrollTop;
    repositionPopup();
  }, { signal });
  input.addEventListener('blur', () => closePopup(), { signal });
  window.addEventListener('resize', () => {
    ensureOverlay();
    renderOverlay();
    repositionPopup();
  }, { signal });
  // 侧栏/看板/检查器开合是容器级布局变化，不触发 window resize——
  // 不监听的话覆盖层按过期宽度渲染（inset:0 中 left+width 生效、right 被忽略），
  // composer 变窄时文字会溢出面板右边框。
  const resizeObserver = typeof ResizeObserver === 'undefined'
    ? null
    : new ResizeObserver(() => {
      ensureOverlay();
      renderOverlay();
      repositionPopup();
    });
  resizeObserver?.observe(input);
  document.addEventListener('click', (e) => {
    const t = e.target as HTMLElement;
    if (popup && !popup.contains(t) && t !== input) closePopup();
  }, { signal });

  // 预取 skills（首启未连后端会失败，prefetchSkills 按秒退避重试），让首条 / 零延迟；
  // 拿到后也补画一次 chip（/中文名 药丸才出现）。
  prefetchSkills();
  return () => {
    if (!bound) return;
    bindingController?.abort();
    resizeObserver?.disconnect();
    unsubscribeWork();
    unsubscribeMode();
    unsubscribeSession();
    bindingController = null;
    if (prefetchTimer != null) window.clearTimeout(prefetchTimer);
    prefetchTimer = null;
    closePopup();
    overlay?.remove();
    overlay = null;
    workTagsHost?.remove();
    workTagsHost = null;
    workTags = [];
    compactMentions = [];
    disabledWorkPreferenceIds.clear();
    if (input) {
      // 合成中途解绑时还原文字透明（overlay 随后即移除，无需清 visibility）
      clearRuntimeStyle(input, 'color');
      input.classList.remove('chat-input-overlay-source');
      input.parentElement?.classList.remove('chat-input-overlay-host');
    }
    input = null;
    composing = false;
    bound = false;
  };
}
