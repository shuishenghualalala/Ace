/**
 * Wiki 知识库页：知识浏览与右栏 Wiki Agent 对话（对齐 web WikiHub）。
 *
 * 数据源：GET /api/wiki/kbs + /api/wiki/pages（brief=1 分页）+ /api/wiki/pages/{id} + /api/wiki/summary
 *         + /api/wiki/graph（Phase 3 图谱，由 features/wiki-graph.ts 消费）
 * 写操作：POST /api/wiki/upload（主进程 gateway:upload IPC）+ /api/wiki/ingest(+cancel)
 *         + DELETE /api/wiki/pages/{id} + DELETE /api/wiki/pages（批量）
 *         + POST /api/wiki/kbs（新建 KB，内联表单）+ POST /api/wiki/init（无 KB 自动初始化）
 *         + DELETE /api/wiki/kbs/{id}（删除 KB，default 不可删）
 *
 * 布局：
 *   1. 页头：KB 选择器（下拉）+ 新建 KB + 上传 + 批量管理（「问 Wiki」已下线：
 *      右栏对话面板常驻，无需入口按钮）
 *   2. 上传任务面板：每个 source 的进度条 + 阶段文案 + 错误态；进度经 WS
 *      wiki_ingest_progress 帧（chat-controller 回调转发）实时更新
 *   3. 左栏列表：分页「加载更多」；条目 = 标题 + 类型徽标 + 更新时间 + 摘要；
 *      单条删除按钮；批量管理模式下条目变 checkbox 选择；
 *      「图谱」视图下左栏替换为图谱画布（features/wiki-graph.ts，本文件只注入容器 + 回调）
 *   3.5 分栏把手：列表 | 详情 | 对话之间可拖拽调宽（localStorage 持久化，双击复位；图谱模式无列表把手）
 *   4. 右栏详情：标题 + 元信息 + Markdown 正文；未选中时显示 KB 概览 / 空态；
 *      Home.md（知识库概览）的「推荐问题」小节会被后处理成提问按钮，
 *      点击直接把问题发给右栏 Wiki Agent（decorateHomeQuestions + [data-wiki-ask] 委托）
 *   5. 最右栏：Wiki Agent 对话面板（features/wiki-agent.ts 挂载，常驻）
 *
 * 边界态：未登录显示登录引导态（不发请求）；没有任何 KB 时自动初始化 default
 * （对齐 web WikiHub，后端幂等，只自动试一次，失败靠重新进入本页重试）。
 *
 * 行为对齐 web 端 WikiHub / WikiTimelineView / WikiFileTree / WikiTypeView / WikiPageView / WikiGraphView；
 * 文件树构建逻辑移植自 web/src/lib/wikiTree.ts（纯逻辑，无 React 依赖）。
 */

import {
  backendApi,
  type WikiKB,
  type WikiPage,
  type WikiPageType,
  type WikiRelationPage,
  type WikiSourcePage,
  type WikiSourceTitles,
  type WikiVaultDocument,
} from '../backend-client';
import { $, escapeHtml, notify, state } from '../state';
import { renderMarkdownHtml } from '../markdown';
import { mountFoldedMarkdown, type FoldedMarkdownHandle } from '../markdown-fold';
import { showConfirmDialog } from '../ui-feedback';
import { __resetWikiGraphForTest, invalidateWikiGraph, mountWikiGraph } from './wiki-graph';
import { mountWikiEditor, type WikiEditorHandle } from './wiki-editor';
import { maybeStartWikiTourOnce, startWikiTour } from './wiki-tour';

// ── Wiki Agent 入口（Phase 4） ──
// 「上传」按钮（打开右栏附件选择）与失败任务「让 AI 处理」共用同一挂点；回调由 index.ts
// 组合根注入（接到 features/wiki-agent.ts 的 openWikiAgent，本文件不 import wiki-agent 内部）。

export interface WikiAgentAssist {
  fileName: string;
  error: string;
  sourceId?: string | null;
}

export interface WikiAgentEntryRequest {
  kbId: string;
  kbName: string;
  /** 反向注入：上传/编译失败上下文，存在时交给专用 Wiki Agent 处理。 */
  assist?: WikiAgentAssist;
  /** 直接发往 Wiki Agent 的提问（Home.md 推荐问题点击）。 */
  prompt?: string;
  /** 聚焦右栏并打开标准 Composer 附件选择。 */
  openAttachment?: boolean;
}

let wikiAgentEntryHandler: ((req: WikiAgentEntryRequest) => void) | null = null;
let wikiAgentPanelRenderer: ((root: HTMLElement, req: WikiAgentEntryRequest) => void) | null = null;
let wikiAgentKbDeletedHandler: ((kbId: string) => void) | null = null;

export function setWikiAgentEntryHandler(fn: ((req: WikiAgentEntryRequest) => void) | null): void {
  wikiAgentEntryHandler = fn;
}

export function setWikiAgentPanelRenderer(
  fn: ((root: HTMLElement, req: WikiAgentEntryRequest) => void) | null,
): void {
  wikiAgentPanelRenderer = fn;
}

export function setWikiAgentKbDeletedHandler(
  fn: ((kbId: string) => void) | null,
): void {
  wikiAgentKbDeletedHandler = fn;
}

function currentKbName(): string {
  return view.kbs.find((k) => k.id === view.kbId)?.name || view.kbId || '';
}

function fireWikiAgentEntry(assist?: WikiAgentAssist, openAttachment = false): void {
  if (!view.kbId) return;
  wikiAgentEntryHandler?.({
    kbId: view.kbId,
    kbName: currentKbName(),
    ...(assist ? { assist } : {}),
    ...(openAttachment ? { openAttachment: true } : {}),
  });
}

/** Home.md 推荐问题点击：把问题直接发给右栏 Wiki Agent。 */
function fireWikiAgentPrompt(prompt: string): void {
  const text = prompt.trim();
  if (!view.kbId || !text) return;
  wikiAgentEntryHandler?.({ kbId: view.kbId, kbName: currentKbName(), prompt: text });
}

/** 列表视图：时间线 / 文件树 / 类型 / 图谱（对齐 web 端 WikiViewMode）。 */
export type WikiListView = 'timeline' | 'tree' | 'type' | 'graph';

const PAGE_LIMIT = 200;
const DEFAULT_EXPANDED_PATHS = ['wiki', 'wiki/sources'] as const;
/** 后端预置的默认知识库 id（不可删除；无 KB 时自动初始化；缺省选中）。 */
const DEFAULT_KB_ID = 'default';
const TUTORIAL_KB_ID = 'tutorial';

// ── 移植自 web/src/lib/wikiTree.ts 的纯逻辑 ──

export interface WikiTreeFolder {
  kind: 'folder';
  name: string;
  path: string;
  children: WikiTreeNode[];
}

export interface WikiTreePage {
  kind: 'page';
  page: WikiPage;
  path: string;
}

export interface WikiTreeDocument {
  kind: 'document';
  name: 'Home.md' | 'index.md';
  path: string;
}

export type WikiTreeNode = WikiTreeFolder | WikiTreePage | WikiTreeDocument;

const VISIBLE_VAULT_FOLDERS = [
  'wiki/entities',
  'wiki/topics',
  'wiki/sources',
  'wiki/sources/articles',
  'wiki/sources/pdfs',
  'wiki/sources/words',
  'wiki/sources/excels',
  'wiki/sources/ppts',
  'wiki/sources/notes',
  'wiki/sources/sessions',
  'wiki/sources/images',
  'wiki/sources/videos',
  'wiki/sources/assets',
  'wiki/comparisons',
  'wiki/synthesis',
] as const;

const FOLDER_ORDER = new Map<string, number>(
  VISIBLE_VAULT_FOLDERS.map((path, index) => [path, index]),
);

/** 页面类型元数据：order 同时驱动文件树排序与类型视图分组顺序（一张表，防双表漂移）。 */
export const TYPE_META: Record<WikiPageType, { order: number; label: string; shortLabel: string }> = {
  entity: { order: 0, label: '关键词', shortLabel: '关键词' },
  topic: { order: 1, label: '话题', shortLabel: '话题' },
  source: { order: 2, label: '来源摘要', shortLabel: '摘要' },
  comparison: { order: 3, label: '对比分析', shortLabel: '对比' },
  synthesis: { order: 4, label: '综合报告', shortLabel: '综合' },
};

const VAULT_FOLDER_LABELS: Record<string, string> = {
  wiki: '知识库',
  'wiki/entities': '关键词',
  'wiki/topics': '话题',
  'wiki/sources': '来源摘要',
  'wiki/sources/articles': '链接与网页',
  'wiki/sources/pdfs': 'PDF',
  'wiki/sources/words': 'Word 文档',
  'wiki/sources/excels': '表格',
  'wiki/sources/ppts': '演示文稿',
  'wiki/sources/notes': '笔记',
  'wiki/sources/sessions': '会话',
  'wiki/sources/images': '图片',
  'wiki/sources/videos': '视频',
  'wiki/sources/assets': '其他附件',
  'wiki/comparisons': '对比',
  'wiki/synthesis': '综合报告',
};

function vaultFolderLabel(path: string, fallback: string): string {
  return VAULT_FOLDER_LABELS[path] ?? fallback;
}

function vaultDocumentLabel(name: 'Home.md' | 'index.md'): string {
  return name === 'Home.md' ? '知识库概览' : '知识导航';
}

export function buildFileTree(pages: WikiPage[]): WikiTreeFolder {
  const root: WikiTreeFolder = { kind: 'folder', name: '', path: '', children: [] };

  const ensureFolder = (folderPath: string): WikiTreeFolder => {
    const parts = folderPath.split('/').filter(Boolean);
    let current = root;
    for (let index = 0; index < parts.length; index++) {
      const name = parts[index];
      const path = parts.slice(0, index + 1).join('/');
      let folder = current.children.find(
        (node): node is WikiTreeFolder => node.kind === 'folder' && node.name === name,
      );
      if (!folder) {
        folder = { kind: 'folder', name, path, children: [] };
        current.children.push(folder);
      }
      current = folder;
    }
    return current;
  };

  for (const path of VISIBLE_VAULT_FOLDERS) ensureFolder(path);
  root.children.push(
    { kind: 'document', name: 'Home.md', path: 'Home.md' },
    { kind: 'document', name: 'index.md', path: 'index.md' },
  );

  for (const page of pages) {
    const parts = (page.file_path || '')
      .split('/')
      .map((s) => s.trim())
      .filter(Boolean);
    if (parts[0] !== 'wiki') continue;
    let current = root;

    for (let i = 0; i < parts.length; i++) {
      const name = parts[i];
      const isFile = i === parts.length - 1;
      const pathSoFar = parts.slice(0, i + 1).join('/');

      if (isFile) {
        current.children.push({ kind: 'page', page, path: pathSoFar });
      } else {
        let folder = current.children.find(
          (n): n is WikiTreeFolder => n.kind === 'folder' && n.name === name,
        );
        if (!folder) {
          folder = { kind: 'folder', name, path: pathSoFar, children: [] };
          current.children.push(folder);
        }
        current = folder;
      }
    }
  }

  sortTree(root);
  return root;
}

function sortTree(node: WikiTreeFolder): void {
  node.children.sort((a, b) => {
    if (a.kind === 'folder' && b.kind !== 'folder') return -1;
    if (a.kind !== 'folder' && b.kind === 'folder') return 1;
    if (a.kind === 'folder' && b.kind === 'folder') {
      const orderA = FOLDER_ORDER.get(a.path) ?? 999;
      const orderB = FOLDER_ORDER.get(b.path) ?? 999;
      if (orderA !== orderB) return orderA - orderB;
      return a.name.localeCompare(b.name, 'zh-CN');
    }
    if (a.kind === 'document' || b.kind === 'document') {
      const leafA = a as WikiTreePage | WikiTreeDocument;
      const leafB = b as WikiTreePage | WikiTreeDocument;
      const nameA = leafA.kind === 'document' ? leafA.name : leafA.page.title;
      const nameB = leafB.kind === 'document' ? leafB.name : leafB.page.title;
      return nameA.localeCompare(nameB, 'en');
    }
    const pa = a as WikiTreePage;
    const pb = b as WikiTreePage;
    const orderA = TYPE_META[pa.page.page_type]?.order ?? 99;
    const orderB = TYPE_META[pb.page.page_type]?.order ?? 99;
    if (orderA !== orderB) return orderA - orderB;
    return pa.page.title.localeCompare(pb.page.title, 'zh-CN');
  });
  for (const child of node.children) {
    if (child.kind === 'folder') sortTree(child);
  }
}

/** file_path 的全部祖先目录路径（选中页面时自动展开）。 */
export function ancestorPaths(filePath: string): string[] {
  const parts = (filePath || '')
    .split('/')
    .map((s) => s.trim())
    .filter(Boolean);
  const paths: string[] = [];
  for (let i = 1; i < parts.length; i++) {
    paths.push(parts.slice(0, i).join('/'));
  }
  return paths;
}

export interface WikiTypeGroup {
  type: WikiPageType;
  label: string;
  pages: WikiPage[];
}

export function groupByType(pages: WikiPage[]): WikiTypeGroup[] {
  const map = new Map<WikiPageType, WikiPage[]>();
  for (const page of pages) {
    const list = map.get(page.page_type) || [];
    list.push(page);
    map.set(page.page_type, list);
  }
  const groups: WikiTypeGroup[] = [];
  const types = (Object.keys(TYPE_META) as WikiPageType[]).sort((a, b) => TYPE_META[a].order - TYPE_META[b].order);
  for (const type of types) {
    const list = map.get(type);
    if (!list || list.length === 0) continue;
    list.sort((a, b) => b.updated_at - a.updated_at);
    groups.push({ type, label: TYPE_META[type].label, pages: list });
  }
  return groups;
}

export interface WikiDateGroup {
  label: string;
  pages: WikiPage[];
}

/**
 * 时间线分桶定义（有序，先匹配先得；「更早」兜底）。label 只在这张表出现一次，
 * 增删分桶只需改这里（此前 label 分散在顺序数组、Record 键与 if/else 链三处）。
 */
const DATE_BUCKETS: Array<{ label: string; contains: (d: Date, now: Date) => boolean }> = [
  { label: '今天', contains: (d, now) => stripTime(d).getTime() === stripTime(now).getTime() },
  {
    label: '昨天',
    contains: (d, now) => {
      const yesterday = stripTime(now);
      yesterday.setDate(yesterday.getDate() - 1);
      return stripTime(d).getTime() === yesterday.getTime();
    },
  },
  { label: '本周', contains: (d, now) => isSameWeek(d, now) },
  { label: '本月', contains: (d, now) => isSameMonth(d, now) },
  { label: '更早', contains: () => true },
];

/** 按更新时间分桶：今天 / 昨天 / 本周 / 本月 / 更早（对齐 web WikiTimelineView）。 */
export function groupPagesByDate(pages: WikiPage[]): WikiDateGroup[] {
  const sorted = [...pages].sort((a, b) => b.updated_at - a.updated_at);
  const now = new Date();
  const groups = DATE_BUCKETS.map((b) => ({ label: b.label, pages: [] as WikiPage[] }));
  for (const page of sorted) {
    const d = new Date(page.updated_at * 1000);
    const idx = DATE_BUCKETS.findIndex((b) => b.contains(d, now));
    groups[idx === -1 ? groups.length - 1 : idx].pages.push(page);
  }
  return groups.filter((g) => g.pages.length > 0);
}

function stripTime(d: Date): Date {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate());
}

function isSameWeek(a: Date, b: Date): boolean {
  const startOfWeek = (d: Date) => {
    const copy = stripTime(d);
    copy.setDate(copy.getDate() - ((copy.getDay() + 6) % 7));
    return copy.getTime();
  };
  return startOfWeek(a) === startOfWeek(b);
}

function isSameMonth(a: Date, b: Date): boolean {
  return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth();
}

/** 从页面摘要或内容中提取简短描述。 */
export function summaryOf(page: WikiPage, maxLen = 140): string {
  const text = (page.summary || page.content || '').trim().replace(/\s+/g, ' ').slice(0, maxLen);
  return text || '（无内容摘要）';
}

/** toLocaleString 每次调用都重建 formatter；列表整屏渲染会调用数百次，缓存复用。 */
const wikiTimeFormatter = new Intl.DateTimeFormat('zh-CN', {
  month: 'short',
  day: 'numeric',
  hour: '2-digit',
  minute: '2-digit',
});

/** 列表条目的更新时间（对齐 web：月-日 时:分）。 */
export function formatWikiTime(ts: number): string {
  if (!ts) return '';
  return wikiTimeFormatter.format(new Date(ts * 1000));
}

/** 统一从 unknown 错误提取 message（notify 文案拼接用）。 */
function errMsg(err: unknown): string {
  return (err as Error).message;
}

// ── 页面视图状态 ──

interface WikiViewState {
  kbs: WikiKB[];
  /** 当前选中 KB；null 表示尚未加载或没有任何 KB。 */
  kbId: string | null;
  pages: WikiPage[];
  pageOffset: number;
  hasMore: boolean;
  loading: boolean;
  loadingMore: boolean;
  view: WikiListView;
  selectedId: string | null;
  selectedDocumentName: 'Home.md' | 'index.md' | null;
  vaultDocument: WikiVaultDocument | null;
  /** vault 文档（Home.md/index.md）加载中标记。 */
  detailLoading: boolean;
  /** 已加载完整正文的页面（pageId → WikiPage）；列表接口只返回 brief。 */
  pageDetails: Record<string, WikiPage>;
  /** 页面详情接口返回的来源摘要页（pageId → 可跳转来源）。 */
  sourcePages: Record<string, WikiSourcePage[]>;
  /** 页面详情接口返回的正向与反向结构化关系。 */
  relationPages: Record<string, WikiRelationPage[]>;
  sourceTitles: WikiSourceTitles;
  /** KB 概览（/api/wiki/summary）；未生成或加载失败时为 null。 */
  kbSummary: { summary: string; page_count?: number | undefined; source_count?: number | undefined; generated_at?: number | undefined; status: string } | null;
  /** 文件树已展开目录路径。 */
  expandedPaths: Set<string>;
  /** 是否已完成首次加载（避免每次切 tab 都打满量请求）。仅加载成功后置位。 */
  loaded: boolean;
  /** KB 列表加载失败标记：区分「真空」与「加载失败」空态文案，且允许下次切 tab 重试。 */
  kbsLoadFailed: boolean;
  /** 批量选择模式：条目显示 checkbox，点击条目切换选中而非打开详情。 */
  selecting: boolean;
  /** 批量模式下已勾选的页面 id。 */
  selectedIds: Set<string>;
}

function initialViewState(): WikiViewState {
  return {
    kbs: [],
    kbId: null,
    pages: [],
    pageOffset: 0,
    hasMore: false,
    loading: false,
    loadingMore: false,
    view: 'timeline',
    selectedId: null,
    selectedDocumentName: null,
    vaultDocument: null,
    detailLoading: false,
    pageDetails: {},
    sourcePages: {},
    relationPages: {},
    sourceTitles: {},
    kbSummary: null,
    expandedPaths: new Set<string>(DEFAULT_EXPANDED_PATHS),
    loaded: false,
    kbsLoadFailed: false,
    selecting: false,
    selectedIds: new Set<string>(),
  };
}

let view: WikiViewState = initialViewState();

/** 加载代际：切 KB / 整页重载后作废旧请求的回包，避免 stale 数据覆盖新列表。 */
let loadSeq = 0;
/** 「无 KB 时自动初始化 default」是否已尝试过（对齐 web WikiHub：只自动试一次，失败靠重新进入本页重试）。 */
let autoInitAttempted = false;
/** 本次登录期间已完成幂等初始化的 KB；用于补齐旧 KB 缺失的 Vault 根文件。 */
const initializedKbIds = new Set<string>();
/** 新建 KB 内联表单状态（renderShell 重建 DOM 时草稿不丢）。 */
let kbCreateOpen = false;
let kbCreateDraft = '';
let kbCreateSubmitting = false;
let wikiChangedBound = false;

// ── 分栏宽度：拖拽 + 双击复位 + localStorage 持久化（列表栏 / 图谱栏 / 对话栏共用一套机制） ──

export interface PaneWidthStore {
  clamp(w: number): number;
  load(): number | null;
  persist(w: number | null): void;
}

/** localStorage 持久化的分栏宽度存取：clamp = 最小/最大宽度 ∩ 视口比例上限；load 缺失或非法时返回 null。 */
export function createPaneWidthStore(opts: { key: string; min: number; max?: number; vwFactor: number }): PaneWidthStore {
  const { key, min, vwFactor } = opts;
  const max = opts.max ?? Number.POSITIVE_INFINITY;
  const clamp = (w: number): number => {
    if (!Number.isFinite(w)) return min;
    const vwCap = Math.max(min, Math.floor(window.innerWidth * vwFactor));
    return Math.max(min, Math.min(Math.min(max, vwCap), Math.round(w)));
  };
  return {
    clamp,
    load() {
      try {
        const raw = localStorage.getItem(key);
        if (!raw) return null;
        const parsed = parseInt(raw, 10);
        return Number.isFinite(parsed) ? clamp(parsed) : null;
      } catch {
        return null;
      }
    },
    persist(w) {
      try {
        if (w == null) localStorage.removeItem(key);
        else localStorage.setItem(key, String(w));
      } catch {
        /* quota / disabled */
      }
    },
  };
}

/**
 * 分栏把手拖拽：mousedown 起监听 document move/up，拖动中只改内联样式（不重渲染不丢事件），
 * mouseup 提交持久化，双击复位。sign = -1 用于把手在面板左缘的场景（向左拖变宽）。
 */
export function bindPaneSash(
  sash: HTMLElement,
  opts: {
    sign?: 1 | -1;
    startWidth: () => number;
    onStart?: () => void;
    onDrag: (w: number) => void;
    onCommit: (w: number) => void;
    onReset: () => void;
  },
): void {
  const sign = opts.sign ?? 1;
  sash.addEventListener('mousedown', (e) => {
    const startX = e.clientX;
    const startW = opts.startWidth();
    let current = startW;
    sash.classList.add('is-dragging');
    document.body.classList.add('wiki-resizing');
    opts.onStart?.();
    const onMove = (ev: MouseEvent): void => {
      current = startW + sign * (ev.clientX - startX);
      opts.onDrag(current);
    };
    const onUp = (): void => {
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
      sash.classList.remove('is-dragging');
      document.body.classList.remove('wiki-resizing');
      opts.onCommit(current);
    };
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
    e.preventDefault();
  });
  sash.addEventListener('dblclick', () => opts.onReset());
}

const WIKI_LIST_DEFAULT_WIDTH = 340;
const listWidthStore = createPaneWidthStore({ key: 'crew.desktop.wikiListWidth.v1', min: 240, max: 640, vwFactor: 0.5 });
/** 图谱栏宽度（仅图谱视图）：null = 与详情栏按 1.5:1 弹性分配；拖拽后固定为像素值并持久化，双击复位回弹性。 */
const graphWidthStore = createPaneWidthStore({ key: 'crew.desktop.wikiGraphWidth.v1', min: 280, vwFactor: 0.7 });

let listWidth = listWidthStore.load() ?? WIKI_LIST_DEFAULT_WIDTH;
let graphWidth: number | null = graphWidthStore.load();
/** 正在加载详情的 pageId（防重复点击重复请求）。 */
const loadingDetails = new Set<string>();

/** 测试钩子：覆盖 view 状态（单测用）。 */
export function __setWikiViewForTest(patch: Partial<WikiViewState>): void {
  view = { ...view, ...patch };
}

/** 视图状态整体复位（登录态变化 / 测试钩子共用，防两处漂移漏清）。 */
function resetWikiViewState(): void {
  view = initialViewState();
  graphWidth = null;
  loadSeq = 0;
  autoInitAttempted = false;
  initializedKbIds.clear();
  kbCreateOpen = false;
  kbCreateDraft = '';
  kbCreateSubmitting = false;
  loadingDetails.clear();
}

/** 测试钩子：重置为初始状态（单测用）。 */
export function __resetWikiViewForTest(): void {
  resetWikiViewState();
  graphWidth = null;
  loadSeq = 0;
  listScrollMemory = null;
  __resetWikiGraphForTest();
}

// ── 渲染 ──

function wikiIcon(name: 'folder' | 'caret', size: number): string {
  if (name === 'folder') {
    return `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.93a2 2 0 0 1-1.66-.9l-.82-1.2A2 2 0 0 0 7.93 2H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2z"/></svg>`;
  }
  return `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m9 18 6-6-6-6"/></svg>`;
}

function typeBadge(type: WikiPageType, long = false): string {
  const meta = TYPE_META[type] ?? { label: type, shortLabel: type };
  return `<span class="wiki-badge wiki-badge--${escapeHtml(type)}">${escapeHtml(long ? meta.label : meta.shortLabel)}</span>`;
}

/** 批量管理模式的操作条（全选 / 删除选中 / 完成），对齐 web WikiHub 批量删除行为。 */
function batchBarHtml(): string {
  // 图谱视图没有条目列表可勾选，隐藏批量条（选择状态保留，切回列表视图恢复）。
  if (!view.selecting || view.view === 'graph') return '';
  const count = view.selectedIds.size;
  return `
    <div class="wiki-batch-bar">
      <button type="button" class="wiki-batch-bar__btn" data-select-all>全选</button>
      <button type="button" class="wiki-batch-bar__btn" data-deselect-all${count === 0 ? ' disabled' : ''}>取消全选</button>
      <span class="wiki-batch-bar__count">已选 ${count} 项</span>
      <button type="button" class="wiki-batch-bar__btn wiki-batch-bar__btn--danger" data-bulk-delete${count === 0 ? ' disabled' : ''}>删除选中</button>
      <button type="button" class="wiki-batch-bar__btn" data-batch-done>完成</button>
    </div>`;
}

/** 批量选择模式的勾选标记与选中态 class（listItemHtml / treeNodesHtml 共用）。 */
function selectionMark(pageId: string): { checkedClass: string; checkHtml: string } {
  if (!view.selecting) return { checkedClass: '', checkHtml: '' };
  return {
    checkedClass: view.selectedIds.has(pageId) ? ' is-checked' : '',
    checkHtml: '<span class="wiki-item__check" aria-hidden="true"></span>',
  };
}

/** 单条删除按钮（批量选择模式下隐藏）。 */
function deletePageBtnHtml(page: WikiPage): string {
  if (view.selecting) return '';
  return `<button type="button" class="wiki-item__delete" data-delete-id="${escapeHtml(page.id)}" data-delete-title="${escapeHtml(page.title)}" title="删除页面" aria-label="删除页面">×</button>`;
}

function listItemHtml(page: WikiPage, compact = false): string {
  const active = page.id === view.selectedId ? ' is-active' : '';
  const { checkedClass, checkHtml } = selectionMark(page.id);
  const tags = page.tags.length
    ? `<span class="wiki-item__tags">${page.tags.map((t) => `<span class="wiki-item__tag">${escapeHtml(t)}</span>`).join('')}</span>`
    : '';
  const time = formatWikiTime(page.updated_at);
  return `
    <li class="wiki-item${compact ? ' wiki-item--compact' : ''}${view.selecting ? ' wiki-item--selecting' : ''}${active}${checkedClass}" data-page-id="${escapeHtml(page.id)}">
      <button type="button" class="wiki-item__main">
        ${checkHtml}
        <span class="wiki-item__title-row">
          <span class="wiki-item__title">${escapeHtml(page.title)}</span>
          ${typeBadge(page.page_type)}
        </span>
        <span class="wiki-item__summary">${escapeHtml(summaryOf(page))}</span>
        <span class="wiki-item__meta">${tags}<span class="wiki-item__time">${escapeHtml(time)}</span></span>
      </button>
      ${deletePageBtnHtml(page)}
    </li>
  `;
}

function timelineViewHtml(): string {
  const groups = groupPagesByDate(view.pages);
  return groups
    .map(
      (g) => `
      <div class="wiki-tl-group">
        <div class="wiki-tl-group__header">${escapeHtml(g.label)}</div>
        <ul class="wiki-list">${g.pages.map((p) => listItemHtml(p)).join('')}</ul>
      </div>`,
    )
    .join('');
}

/** 文件树缩进：每层 16px + 基础 6px（folder toggle 与 page label 共用同一公式保持对齐）。 */
const TREE_INDENT_STEP_PX = 16;
const TREE_INDENT_BASE_PX = 6;

function treeIndentPx(depth: number): number {
  return depth * TREE_INDENT_STEP_PX + TREE_INDENT_BASE_PX;
}

/** 递归统计文件夹下的笔记数量（含子文件夹；document 节点不计入）。 */
function countFolderPages(folder: WikiTreeFolder): number {
  let count = 0;
  for (const child of folder.children) {
    if (child.kind === 'page') count += 1;
    else if (child.kind === 'folder') count += countFolderPages(child);
  }
  return count;
}

function treeNodesHtml(nodes: WikiTreeNode[], depth: number): string {
  return nodes
    .map((node) => {
      if (node.kind === 'folder') {
        const open = view.expandedPaths.has(node.path);
        const children = open && node.children.length > 0 ? treeNodesHtml(node.children, depth + 1) : '';
        const count = countFolderPages(node);
        const countHtml = count > 0 ? `<span class="wiki-tree__folder-count">${count}</span>` : '';
        return `
          <li class="wiki-tree__folder">
            <button type="button" class="wiki-tree__folder-toggle${open ? ' is-open' : ''}" data-tree-path="${escapeHtml(node.path)}" style="padding-left: ${treeIndentPx(depth)}px">
              <span class="wiki-tree__caret">${wikiIcon('caret', 12)}</span>
              <span class="wiki-tree__folder-icon">${wikiIcon('folder', 14)}</span>
              <span class="wiki-tree__folder-name">${escapeHtml(vaultFolderLabel(node.path, node.name))}</span>
              ${countHtml}
            </button>
            ${children ? `<ul class="wiki-tree__list">${children}</ul>` : ''}
          </li>`;
      }
      if (node.kind === 'document') {
        const active = node.name === view.selectedDocumentName ? ' is-active' : '';
        return `
          <li class="wiki-tree__item wiki-tree__item--document${active}" data-vault-document="${escapeHtml(node.name)}">
            <button type="button" class="wiki-tree__label" style="padding-left: ${depth * 16 + 6}px">
              <span class="wiki-badge">文件</span>
              <span class="wiki-tree__title">${escapeHtml(vaultDocumentLabel(node.name))}</span>
            </button>
          </li>`;
      }
      const active = node.page.id === view.selectedId ? ' is-active' : '';
      const { checkedClass, checkHtml } = selectionMark(node.page.id);
      // 删除按钮是 <li> 的独立子元素（button 不能嵌套 button），
      // 由 CSS 绝对定位到文件名左侧的缩进空隙里，悬停时淡入。
      return `
        <li class="wiki-tree__item${view.selecting ? ' wiki-item--selecting' : ''}${active}${checkedClass}" data-page-id="${escapeHtml(node.page.id)}" style="--tree-indent: ${treeIndentPx(depth)}px">
          <button type="button" class="wiki-tree__label" style="padding-left: var(--tree-indent)">
            ${checkHtml}
            ${typeBadge(node.page.page_type)}
            <span class="wiki-tree__title">${escapeHtml(node.page.title)}</span>
          </button>
          ${deletePageBtnHtml(node.page)}
        </li>`;
    })
    .join('');
}

function fileTreeViewHtml(): string {
  const root = buildFileTree(view.pages);
  return `<ul class="wiki-tree__list wiki-tree__list--root">${treeNodesHtml(root.children, 0)}</ul>`;
}

function typeViewHtml(): string {
  const groups = groupByType(view.pages);
  return groups
    .map(
      (g) => `
      <section class="wiki-type-section">
        <h4 class="wiki-type-section__title">${escapeHtml(g.label)}<span class="wiki-type-section__count">${g.pages.length}</span></h4>
        <ul class="wiki-list">${g.pages.map((p) => listItemHtml(p, true)).join('')}</ul>
      </section>`,
    )
    .join('');
}

function listViewHtml(): string {
  if (view.view === 'tree') return fileTreeViewHtml();
  if (view.pages.length === 0) return '';
  if (view.view === 'type') return typeViewHtml();
  return timelineViewHtml();
}

function loadMoreHtml(): string {
  if (!view.hasMore || view.pages.length === 0) return '';
  const label = view.loadingMore ? '加载中…' : '加载更多';
  return `
    <div class="wiki-load-more">
      <button type="button" class="wiki-load-more__btn" data-load-more${view.loadingMore ? ' disabled' : ''}>${label}</button>
    </div>`;
}

/** 新建 KB 内联表单（对齐 web WikiHub 的 window.prompt 流程；Electron 不支持 window.prompt，改内联表单）。 */
function kbCreateHtml(): string {
  if (!kbCreateOpen) return '';
  return `
    <div class="wiki-kb-create">
      <input type="text" class="wiki-kb-create__input" data-kb-create-input
        placeholder="新建知识库 ID（支持中文，勿含 \ / : * ? 等字符）" maxlength="64"
        value="${escapeHtml(kbCreateDraft)}"${kbCreateSubmitting ? ' disabled' : ''} />
      <button type="button" class="hub-refresh-btn wiki-kb-create__submit" data-kb-create-submit${kbCreateSubmitting ? ' disabled' : ''}>${kbCreateSubmitting ? '创建中…' : '创建'}</button>
      <button type="button" class="hub-refresh-btn" data-kb-create-cancel${kbCreateSubmitting ? ' disabled' : ''}>取消</button>
    </div>`;
}

export interface WikiDetailArticleOptions {
  sourcePages?: WikiSourcePage[];
  relationPages?: WikiRelationPage[];
}

const RELATION_LABELS: Record<string, string> = {
  related: '相关',
  uses: '使用',
  depends_on: '依赖',
  part_of: '属于',
  contrasts_with: '对比',
  describes: '描述',
  covers: '涵盖',
  references: '引用',
  mentions: '提及',
};

function relationLabel(item: WikiRelationPage): string {
  const label = RELATION_LABELS[item.relation] || item.relation;
  if (item.direction === 'outgoing') return label;
  return {
    describes: '描述本页',
    covers: '涵盖本页',
    depends_on: '依赖本页',
    part_of: '包含本页',
    uses: '使用本页',
    references: '引用本页',
    mentions: '提及本页',
  }[item.relation] || `反向${label}`;
}

/** 页面详情 article HTML（Wiki 页右栏与 Phase 4 对话流 overlay 共用）。所有插值 escapeHtml。 */
export function wikiDetailArticleHtml(page: WikiPage, opts: WikiDetailArticleOptions): string {
  const sourcePills = opts.sourcePages?.length
    ? opts.sourcePages.map((target) => `
        <button type="button" class="wiki-badge wiki-source-pill"
          data-source-page-id="${escapeHtml(target.id)}">${escapeHtml(target.title)}</button>`).join('')
    : '<span class="wiki-property__empty">无</span>';
  const visibleRelations = page.page_type === 'source'
    ? []
    : (opts.relationPages ?? []).filter((target) => target.page_type !== 'source');
  const relationCards = visibleRelations.length
    ? visibleRelations.map((target) => `
        <button type="button" class="wiki-relation-card"
          data-related-page-id="${escapeHtml(target.id)}">
          <span class="wiki-relation-card__title">${escapeHtml(target.title)}</span>
          <span class="wiki-relation-card__meta">${escapeHtml(TYPE_META[target.page_type]?.label || target.page_type)} · ${escapeHtml(relationLabel(target))}</span>
        </button>`).join('')
    : '<span class="wiki-property__empty">暂无相关页面</span>';
  const propertyRow = (label: string, icon: string, value: string): string => `
    <label class="wiki-property">
      <span class="wiki-property__name"><span aria-hidden="true">${icon}</span>${label}</span>
      ${value}
    </label>`;
  return `
    <article class="wiki-detail">
      <header class="wiki-detail__header">
        <input class="wiki-detail__title" data-wiki-title value="${escapeHtml(page.title)}"
          aria-label="页面标题" autocomplete="off" />
        <div class="wiki-detail__save-state" data-wiki-save-state>已保存</div>
        <div class="wiki-properties" aria-label="页面属性">
          ${propertyRow('类型', '◈', `<span class="wiki-property__value">${typeBadge(page.page_type, true)}</span>`)}
          ${propertyRow('创建时间', '◷', `<span class="wiki-property__value">${escapeHtml(formatWikiTime(page.created_at))}</span>`)}
          ${propertyRow('更新时间', '◷', `<span class="wiki-property__value">${escapeHtml(formatWikiTime(page.updated_at))}</span>`)}
          ${propertyRow('来源', '⌁', `<span class="wiki-property__links">${sourcePills}</span>`)}
        </div>
      </header>
      <div class="wiki-editor" data-wiki-editor></div>
      ${page.page_type === 'source' ? '' : `
        <section class="wiki-related-pages" aria-label="相关页面">
          <h3>相关页面</h3>
          <div class="wiki-related-pages__grid">${relationCards}</div>
        </section>`}
    </article>`;
}

/**
 * 为 wikiDetailArticleHtml 生成的正文挂载点做增量渲染（renderShell 与对话流 overlay 共用）。
 * 返回清理句柄；无挂载点或无正文时返回 null。
 */
export function mountWikiDetailFold(
  root: ParentNode,
  page: WikiPage | null | undefined,
): FoldedMarkdownHandle | null {
  const target = root.querySelector<HTMLElement>('[data-wiki-fold-content]');
  if (!target || !page?.content) return null;
  return mountFoldedMarkdown(target, page.content);
}

/** Home.md「推荐问题」小节标题（与后端 store/_filesystem.py 的 _HOME_QUESTIONS_HEADING 同步）。 */
const HOME_QUESTIONS_HEADING = '推荐问题';

/**
 * 把 Home.md 渲染结果里的「推荐问题」h2+ul 替换成可点击的提问按钮组
 * （仿 NotebookLM 首页版式）；按钮点击经 [data-wiki-ask] 委托发给 Wiki Agent。
 * 该小节紧贴导读、位于文档前部，mountFoldedMarkdown 首屏渲染后即可同步处理。
 */
export function decorateHomeQuestions(container: HTMLElement): void {
  const heading = Array.from(container.querySelectorAll('h2'))
    .find((h) => h.textContent?.trim() === HOME_QUESTIONS_HEADING);
  if (!heading) return;
  const list = heading.nextElementSibling;
  const items = list?.tagName === 'UL'
    ? Array.from(list.querySelectorAll('li'))
        .map((li) => li.textContent?.trim() ?? '')
        .filter(Boolean)
    : [];
  if (items.length === 0) return;
  const box = document.createElement('div');
  box.className = 'wiki-ask-chips';
  for (const question of items) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'wiki-ask-chip';
    btn.dataset.wikiAsk = question;
    btn.textContent = question;
    box.appendChild(btn);
  }
  heading.replaceWith(box);
  list?.remove();
}

function detailHtml(): string {
  if (view.selectedDocumentName) {
    if (view.detailLoading || !view.vaultDocument) {
      return `<div class="wiki-detail__empty"><p class="wiki-detail__empty-hint">加载文档中…</p></div>`;
    }
    const isHome = view.vaultDocument.name === 'Home.md';
    return `
      <article class="wiki-detail${isHome ? ' wiki-home-document' : ''}">
        <header class="wiki-detail__header">
          <div class="wiki-detail__badges"><span class="wiki-badge">${isHome ? '概览' : '文件'}</span></div>
          <h2 class="wiki-detail__title">${escapeHtml(vaultDocumentLabel(view.vaultDocument.name))}</h2>
          <div class="wiki-detail__meta">
            <span>更新于 ${escapeHtml(formatWikiTime(view.vaultDocument.updated_at))}</span>
          </div>
        </header>
        <div class="md-body chat-markdown wiki-detail__content" data-wiki-fold-content></div>
      </article>`;
  }
  if (!view.selectedId) {
    const summary = view.kbSummary
      ? `<div class="wiki-overview">
          <div class="wiki-overview__title">知识库概览</div>
          <div class="md-body chat-markdown wiki-overview__body">${renderMarkdownHtml(view.kbSummary.summary)}</div>
          ${view.kbSummary.page_count != null || view.kbSummary.source_count != null
            ? `<div class="wiki-overview__meta">${view.kbSummary.page_count != null ? `${view.kbSummary.page_count} 个页面` : ''}${view.kbSummary.page_count != null && view.kbSummary.source_count != null ? ' · ' : ''}${view.kbSummary.source_count != null ? `${view.kbSummary.source_count} 个来源` : ''}</div>`
            : ''}
        </div>`
      : '';
    return `
      <div class="wiki-detail__empty">
        ${summary}
        <p class="wiki-detail__empty-hint">选择左侧页面查看详情，或在右侧对话栏基于知识库提问</p>
      </div>`;
  }
  if (loadingDetails.has(view.selectedId) && !view.pageDetails[view.selectedId]) {
    return `<div class="wiki-detail__empty"><p class="wiki-detail__empty-hint">加载页面详情中…</p></div>`;
  }
  const page = view.pageDetails[view.selectedId] ?? view.pages.find((p) => p.id === view.selectedId);
  if (!page) {
    return `<div class="wiki-detail__empty"><p class="wiki-detail__empty-hint">选择左侧页面查看详情</p></div>`;
  }
  return wikiDetailArticleHtml(page, {
    sourcePages: view.sourcePages[page.id],
    relationPages: view.relationPages[page.id],
  });
}

/** 当前详情正文的增量渲染句柄（详情子树重建时先 dispose，防 observer 泄漏）。 */
let detailFoldHandle: FoldedMarkdownHandle | null = null;
let detailEditorHandle: WikiEditorHandle | null = null;
let saveTimer: ReturnType<typeof setTimeout> | null = null;
let localSaveInFlight = false;
let ignoreWikiChangedUntil = 0;
/** 详情有未保存的真实编辑（仅 scheduleWikiPageSave 置位；看一眼不置位，避免浏览也刷 updated_at）。 */
let detailDirty = false;

function setWikiSaveState(stateValue: 'dirty' | 'saving' | 'saved' | 'error'): void {
  const target = document.querySelector<HTMLElement>('#wiki-page-root [data-wiki-save-state]');
  if (!target) return;
  target.dataset.state = stateValue;
  target.textContent = {
    dirty: '等待保存…',
    saving: '保存中…',
    saved: '已保存',
    error: '保存失败，将在下次修改时重试',
  }[stateValue];
}

function pageDraftFromDom(page: WikiPage, content: string): WikiPage {
  const root = document.querySelector<HTMLElement>('#wiki-page-root');
  const value = (selector: string): string =>
    root?.querySelector<HTMLInputElement>(selector)?.value ?? '';
  return {
    ...page,
    title: value('[data-wiki-title]').trim() || page.title,
    content,
  };
}

async function saveWikiPageDraft(pageId: string): Promise<void> {
  if (!view.kbId) return;
  const current = view.pageDetails[pageId];
  if (!current || view.selectedId !== pageId) return;
  const draft = pageDraftFromDom(current, detailEditorHandle?.flush() ?? current.content ?? '');
  setWikiSaveState('saving');
  localSaveInFlight = true;
  try {
    const result = await backendApi.wikiUpdatePage(pageId, {
      title: draft.title,
      content: draft.content ?? '',
      tags: draft.tags,
      sources: draft.sources,
      relations: draft.relations ?? [],
    }, view.kbId);
    view.pageDetails = { ...view.pageDetails, [pageId]: result.page };
    view.sourcePages = {
      ...view.sourcePages,
      [pageId]: result.source_pages ?? view.sourcePages[pageId] ?? [],
    };
    view.relationPages = {
      ...view.relationPages,
      [pageId]: result.relation_pages ?? view.relationPages[pageId] ?? [],
    };
    const listIndex = view.pages.findIndex((page) => page.id === pageId);
    if (listIndex >= 0) view.pages[listIndex] = { ...view.pages[listIndex], ...result.page };
    view.sourceTitles = { ...view.sourceTitles, ...(result.source_titles || {}) };
    ignoreWikiChangedUntil = Date.now() + 1200;
    detailDirty = false;
    setWikiSaveState('saved');
    invalidateWikiGraph();
  } catch (error) {
    setWikiSaveState('error');
    notify(`保存 Wiki 页面失败：${errMsg(error)}`);
  } finally {
    localSaveInFlight = false;
  }
}

function scheduleWikiPageSave(pageId: string): void {
  detailDirty = true;
  setWikiSaveState('dirty');
  if (saveTimer) clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    saveTimer = null;
    void saveWikiPageDraft(pageId);
  }, 700);
}

function disposeDetailEditor(): void {
  if (saveTimer) {
    clearTimeout(saveTimer);
    saveTimer = null;
  }
  detailEditorHandle?.destroy();
  detailEditorHandle = null;
  detailDirty = false;
}

async function resolveAndOpenWikiPage(title: string): Promise<boolean> {
  const normalized = title.trim().toLocaleLowerCase();
  const local = view.pages.find((page) =>
    [page.title, ...page.aliases].some((value) => value.trim().toLocaleLowerCase() === normalized),
  );
  if (local) {
    view.selectedDocumentName = null;
    view.vaultDocument = null;
    selectWikiPage(local.id, { expandTree: true });
    return true;
  }
  if (!view.kbId) return false;
  try {
    const result = await backendApi.wikiSearch(title, view.kbId, 8);
    const target = result.pages.find((page) =>
      [page.title, ...page.aliases].some((value) => value.trim().toLocaleLowerCase() === normalized),
    );
    if (!target) {
      notify(`未找到 Wiki 页面：${title}`);
      return false;
    }
    if (!view.pages.some((page) => page.id === target.id)) view.pages.push(target);
    view.pageDetails = { ...view.pageDetails, [target.id]: target };
    view.sourceTitles = { ...view.sourceTitles, ...(result.source_titles || {}) };
    view.selectedDocumentName = null;
    view.vaultDocument = null;
    selectWikiPage(target.id, { expandTree: true });
    return true;
  } catch (error) {
    notify(`打开 Wiki 页面失败：${errMsg(error)}`);
    return false;
  }
}

/**
 * 详情栏内容签名：renderShell 全量重建时，签名未变则保留现有详情子树
 * （fold 增量渲染的 observer 与已解析正文一并存活，避免每次点击都重跑 markdown 解析）。
 */
interface DetailSig {
  selectedId: string | null;
  page: WikiPage | null;
  sourceTitles: WikiSourceTitles;
  kbSummary: { summary: string; page_count?: number | undefined; source_count?: number | undefined; generated_at?: number | undefined; status: string } | null;
  loading: boolean;
}

function currentDetailSig(): DetailSig {
  const selectedId = view.selectedId;
  return {
    selectedId,
    page: selectedId ? view.pageDetails[selectedId] ?? view.pages.find((p) => p.id === selectedId) ?? null : null,
    sourceTitles: view.sourceTitles,
    kbSummary: selectedId ? null : view.kbSummary,
    loading: selectedId ? loadingDetails.has(selectedId) : false,
  };
}

function sameDetailSig(a: DetailSig, b: DetailSig): boolean {
  return (
    a.selectedId === b.selectedId &&
    a.page === b.page &&
    a.sourceTitles === b.sourceTitles &&
    a.kbSummary?.summary === b.kbSummary?.summary &&
    a.kbSummary?.page_count === b.kbSummary?.page_count &&
    a.kbSummary?.source_count === b.kbSummary?.source_count &&
    a.loading === b.loading
  );
}

let lastDetailSig: DetailSig | null = null;

/** 列表滚动记忆：renderShell 全量重建后按 视图+KB 恢复 scrollTop，避免点击条目滚动条跳回顶部。 */
let listScrollMemory: { key: string; top: number } | null = null;

/** 左栏内联样式：列表视图固定持久化宽度；图谱视图未拖拽时弹性分配（无内联样式），拖拽后固定像素。 */
function listPaneStyleAttr(): string {
  if (view.view !== 'graph') return ` style="width: ${listWidth}px"`;
  return graphWidth != null ? ` style="width: ${graphWidth}px; flex: 0 0 auto"` : '';
}

function renderShell(): void {
  const root = $('#wiki-page-root');
  if (!root) return;

  const kbOptions = view.kbs.length
    ? view.kbs
        .map(
          (kb) =>
            `<option value="${escapeHtml(kb.id)}"${kb.id === view.kbId ? ' selected' : ''}>${escapeHtml(kb.name)}</option>`,
        )
        .join('')
    : '<option value="" selected>暂无知识库</option>';

  const viewTabs: Array<{ key: WikiListView; label: string }> = [
    { key: 'timeline', label: '时间线' },
    { key: 'tree', label: '文件树' },
    { key: 'type', label: '类型' },
    { key: 'graph', label: '图谱' },
  ];
  const tabs = viewTabs
    .map(
      (t) =>
        `<button type="button" class="hub-segment__item${view.view === t.key ? ' is-active' : ''}" data-wiki-view="${t.key}">${t.label}</button>`,
    )
    .join('');

  let body: string;
  if (!view.kbId) {
    // 加载失败与「真空」分开文案：失败时引导检查后重新进入本页重试（Phase 2 修复）。
    const emptyTitle = view.kbsLoadFailed ? '知识库加载失败' : view.loading ? '知识库加载中…' : '暂无知识库';
    const emptyDesc = view.kbsLoadFailed
      ? '请检查后端服务连接后，重新进入本页重试。'
      : view.loading
        ? '正在读取知识库列表。'
        : '请重新进入本页重试。';
    body = `
      <div class="v2-empty">
        <div class="v2-empty__icon">∅</div>
        <div class="v2-empty__title">${emptyTitle}</div>
        <div class="v2-empty__desc">${emptyDesc}</div>
      </div>`;
  } else {
    // 图谱视图：左栏列表区域整体替换为图谱画布（挂载点由 mountWikiGraph 接管）。
    const graphMode = view.view === 'graph';
    const listBody = graphMode
      ? '<div class="wiki-graph-mount" data-graph-mount></div>'
      : view.loading && view.pages.length === 0
        ? '<div class="wiki-list__loading">页面加载中…</div>'
        : view.pages.length === 0 && view.view !== 'tree'
          ? `<div class="wiki-list__empty wiki-list__empty--guide">
              <div class="wiki-list__empty-art" aria-hidden="true">
                <svg class="wiki-list__empty-arc" viewBox="0 0 120 90" fill="none">
                  <path d="M14 84 C 30 40, 66 26, 104 16" stroke="currentColor" stroke-width="1.5" stroke-dasharray="5 5" stroke-linecap="round"/>
                </svg>
                <svg class="wiki-list__empty-plane" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M22 2 11 13"/><path d="M22 2 15 22l-4-9-9-4 20-7z"/>
                </svg>
              </div>
              <p class="wiki-list__empty-text">知识库还没有内容</p>
              <p class="wiki-list__empty-hint">点击右上角「上传」，或直接拖拽文件到右侧问答栏</p>
            </div>`
          : `${listViewHtml()}${loadMoreHtml()}`;
    body = `
      <div class="wiki-body${graphMode ? ' wiki-body--graph' : ''}">
        <div class="wiki-list-pane${graphMode ? ' wiki-list-pane--graph' : ''}"${listPaneStyleAttr()}>
          ${batchBarHtml()}
          <nav class="hub-segment wiki-view-tabs" aria-label="列表视图">${tabs}</nav>
          <div class="wiki-list-scroll${graphMode ? ' wiki-list-scroll--graph' : ''}">${listBody}</div>
        </div>
        <div class="wiki-sash" data-wiki-sash role="separator" aria-orientation="vertical" title="拖拽调整左栏宽度，双击复位"></div>
        <div class="wiki-detail-pane">${detailHtml()}</div>
        <div class="wiki-sash" data-wiki-agent-sash role="separator" aria-orientation="vertical" title="拖拽调整对话栏宽度，双击复位"></div>
        <aside class="wiki-agent-pane" data-wiki-agent-panel aria-label="Wiki Agent 对话"></aside>
      </div>`;
  }

  const uploadDisabled = !view.kbId;
  const batchToggle =
    view.kbId && !view.selecting && view.pages.length > 0 && view.view !== 'graph'
      ? '<button type="button" class="hub-refresh-btn" data-batch-toggle title="批量选择页面以批量删除">批量管理</button>'
      : '';
  // 重建代价高的三棵子树在状态未变时保留活节点，避免每次点击都推倒重来：
  // 对话面板（整段会话重渲染 + markdown 重解析 + 强制滚底）、详情栏（fold observer 与
  // 已解析正文）、图谱画布（SVG 全量重建 + 逐节点重绑事件，wiki-graph 内部另有签名比对）。
  const liveAgentPanel = root.querySelector<HTMLElement>('[data-wiki-agent-panel]');
  const keepAgentPanel = view.kbId && liveAgentPanel?.dataset.kbId === view.kbId ? liveAgentPanel : null;
  const keepAgentSash = keepAgentPanel ? root.querySelector<HTMLElement>('[data-wiki-agent-sash]') : null;
  const detailSigNow = currentDetailSig();
  const liveDetailPane = root.querySelector<HTMLElement>('.wiki-detail-pane');
  const keepDetailPane = liveDetailPane && !view.selectedDocumentName && lastDetailSig && sameDetailSig(lastDetailSig, detailSigNow) ? liveDetailPane : null;
  const keepGraphMount = view.view === 'graph' ? root.querySelector<HTMLElement>('[data-graph-mount]') : null;
  const liveListScroll = root.querySelector<HTMLElement>('.wiki-list-scroll');
  if (liveListScroll && listScrollMemory) listScrollMemory.top = liveListScroll.scrollTop;
  root.innerHTML = `
    <div class="page-shell page-shell--wiki">
      <header class="page-header page-header--hub">
        <div class="page-header__copy">
          <h1 class="page-header__title">Wiki <span class="accent">知识库</span></h1>
          <p class="page-header__desc">大模型自动整理的本地知识库笔记，按时间线、文件夹或类型浏览。</p>
        </div>
        <div class="page-header__actions">
          <select id="wiki-kb-select" class="wiki-kb-select" title="选择知识库" aria-label="选择知识库"${view.kbs.length === 0 ? ' disabled' : ''}>${kbOptions}</select>
          <button type="button" class="hub-refresh-btn" data-kb-create-toggle title="新建知识库">新建</button>
          <button type="button" class="hub-refresh-btn" data-kb-delete title="删除当前知识库、原始素材及专属 Wiki 问答历史（内置知识库不可删）"${!view.kbId || view.kbId === DEFAULT_KB_ID || view.kbId === TUTORIAL_KB_ID ? ' disabled' : ''}>删除</button>
          <button type="button" class="hub-refresh-btn" data-upload title="上传文件到知识库"${uploadDisabled ? ' disabled' : ''}>上传</button>
          ${batchToggle}
          <button type="button" class="hub-refresh-btn hub-refresh-btn--help" data-wiki-tour title="使用导览" aria-label="使用导览">?</button>
        </div>
      </header>
      ${kbCreateHtml()}
      ${body}
    </div>
  `;
  // 占位节点随 innerHTML 重新生成，状态未变的子树换回复用。
  let agentPanelKept = false;
  if (keepAgentPanel) {
    const placeholder = root.querySelector<HTMLElement>('[data-wiki-agent-panel]');
    if (placeholder) {
      if (keepAgentSash) root.querySelector<HTMLElement>('[data-wiki-agent-sash]')?.replaceWith(keepAgentSash);
      placeholder.replaceWith(keepAgentPanel);
      agentPanelKept = true;
    }
  }
  const detailPlaceholder = root.querySelector<HTMLElement>('.wiki-detail-pane');
  const detailKept = !!(keepDetailPane && detailPlaceholder);
  if (keepDetailPane && detailPlaceholder) detailPlaceholder.replaceWith(keepDetailPane);
  if (keepGraphMount) root.querySelector<HTMLElement>('[data-graph-mount]')?.replaceWith(keepGraphMount);
  const listScrollKey = `${view.kbId ?? ''}:${view.view}`;
  const listScroll = root.querySelector<HTMLElement>('.wiki-list-scroll');
  if (listScroll && listScrollMemory?.key === listScrollKey) listScroll.scrollTop = listScrollMemory.top;
  listScrollMemory = { key: listScrollKey, top: listScroll?.scrollTop ?? 0 };
  bindEvents();
  // Wiki 页面使用常驻 TipTap 编辑器；Home/index 是系统文档，继续只读增量渲染。
  if (!detailKept || view.selectedDocumentName) {
    detailFoldHandle?.dispose();
    detailFoldHandle = null;
    disposeDetailEditor();
    if (view.selectedId) {
      const page = view.pageDetails[view.selectedId] ?? view.pages.find((p) => p.id === view.selectedId);
      const target = root.querySelector<HTMLElement>('[data-wiki-editor]');
      if (target && page?.content !== undefined) {
        detailEditorHandle = mountWikiEditor({
          element: target,
          markdown: page.content || '',
          onChange: () => scheduleWikiPageSave(page.id),
          onWikiLink: (title) => void resolveAndOpenWikiPage(title),
        });
      }
    } else if (view.vaultDocument?.content) {
      const target = root.querySelector<HTMLElement>('[data-wiki-fold-content]');
      if (target) {
        detailFoldHandle = mountFoldedMarkdown(target, view.vaultDocument.content);
        if (view.vaultDocument.name === 'Home.md') decorateHomeQuestions(target);
      }
    }
  }
  if (view.selectedId) {
    const target = root.querySelector<HTMLElement>('[data-wiki-editor]');
    const page = view.pageDetails[view.selectedId] ?? view.pages.find((item) => item.id === view.selectedId);
    if (target && page?.content !== undefined && target.childElementCount === 0) {
      detailEditorHandle?.destroy();
      detailEditorHandle = mountWikiEditor({
        element: target,
        markdown: page.content || '',
        onChange: () => scheduleWikiPageSave(page.id),
        onWikiLink: (title) => void resolveAndOpenWikiPage(title),
      });
    }
  }
  lastDetailSig = detailPlaceholder ? detailSigNow : null;
  if (view.kbId && !agentPanelKept) {
    const panel = root.querySelector<HTMLElement>('[data-wiki-agent-panel]');
    if (panel) {
      wikiAgentPanelRenderer?.(panel, { kbId: view.kbId, kbName: currentKbName() });
    }
  }
  // 图谱视图：renderShell 重建 DOM 后重新挂载（图谱模块自管数据/布局/视口状态，重挂载不丢；
  // 画布节点被保留且状态未变时 mountWikiGraph 内部 no-op）。
  if (view.kbId && view.view === 'graph') {
    const mount = root.querySelector<HTMLElement>('[data-graph-mount]');
    if (mount) {
      mountWikiGraph(mount, view.kbId, {
        onSelectPage: selectWikiPage,
        getSelectedId: () => view.selectedId,
      });
    }
  }
}

// ── 数据加载 ──

async function loadKbs(): Promise<void> {
  const seq = ++loadSeq;
  try {
    let res = await backendApi.wikiKBs();
    if (seq !== loadSeq) return;
    // 对齐 web WikiHub：没有任何知识库时自动初始化 default（后端幂等，只自动试一次，
    // 失败落入失败空态，由用户重新进入本页触发重试）。
    if ((res.kbs ?? []).length === 0 && !autoInitAttempted) {
      autoInitAttempted = true;
      try {
        await backendApi.wikiInit(DEFAULT_KB_ID);
        initializedKbIds.add(DEFAULT_KB_ID);
        res = await backendApi.wikiKBs();
        if (seq !== loadSeq) return;
      } catch (initErr) {
        if (seq !== loadSeq) return;
        view.kbs = [];
        view.kbId = null;
        view.kbsLoadFailed = true;
        notify(`初始化默认知识库失败：${errMsg(initErr)}`);
        return;
      }
    }
    view.kbs = res.kbs ?? [];
    view.kbsLoadFailed = false;
    // 默认选中 default KB；没有 default 则选第一个。
    if (view.kbs.length === 0) {
      view.kbId = null;
      return;
    }
    const currentValid = view.kbId && view.kbs.some((k) => k.id === view.kbId);
    if (!currentValid) {
      view.kbId = (view.kbs.find((k) => k.id === DEFAULT_KB_ID) ?? view.kbs[0]).id;
    }
    // 旧 KB 可能早于 Obsidian Vault 布局创建；选择后执行一次幂等初始化，
    // 确保 Home.md / index.md 以及当前目录骨架真实存在。
    if (view.kbId && !initializedKbIds.has(view.kbId)) {
      await backendApi.wikiInit(view.kbId);
      initializedKbIds.add(view.kbId);
    }
  } catch (err) {
    if (seq !== loadSeq) return;
    view.kbs = [];
    view.kbId = null;
    view.kbsLoadFailed = true;
    notify(`加载知识库失败：${errMsg(err)}`);
  }
}

async function loadPages(): Promise<void> {
  if (!view.kbId) {
    // KB 为空或加载失败：复位 loading 并重绘，否则页面停在「知识库加载中…」空转。
    if (view.loading) {
      view.loading = false;
      renderShell();
    }
    return;
  }
  const seq = loadSeq;
  view.loading = true;
  renderShell();
  try {
    const res = await backendApi.wikiPages({ limit: PAGE_LIMIT, offset: 0, kb_id: view.kbId, brief: true });
    if (seq !== loadSeq) return;
    view.pages = res.pages ?? [];
    view.pageOffset = view.pages.length;
    view.hasMore = view.pages.length >= PAGE_LIMIT;
    view.sourceTitles = res.source_titles || {};
    if (view.selectedId && !view.pages.some((p) => p.id === view.selectedId)) {
      view.selectedId = null;
    }
  } catch (err) {
    if (seq !== loadSeq) return;
    notify(`加载 Wiki 页面失败：${errMsg(err)}`);
  } finally {
    if (seq === loadSeq) view.loading = false;
  }
  renderShell();
}

async function loadMorePages(): Promise<void> {
  if (!view.kbId || view.loadingMore || !view.hasMore) return;
  const seq = loadSeq;
  view.loadingMore = true;
  renderShell();
  try {
    const res = await backendApi.wikiPages({ limit: PAGE_LIMIT, offset: view.pageOffset, kb_id: view.kbId, brief: true });
    if (seq !== loadSeq) return;
    const seen = new Set(view.pages.map((p) => p.id));
    for (const p of res.pages ?? []) {
      if (!seen.has(p.id)) view.pages.push(p);
    }
    view.pageOffset += (res.pages ?? []).length;
    view.hasMore = (res.pages ?? []).length >= PAGE_LIMIT;
    view.sourceTitles = { ...view.sourceTitles, ...(res.source_titles || {}) };
  } catch (err) {
    if (seq !== loadSeq) return;
    notify(`加载更多页面失败：${errMsg(err)}`);
  } finally {
    if (seq === loadSeq) view.loadingMore = false;
  }
  renderShell();
}

async function loadPageDetail(pageId: string): Promise<void> {
  if (!view.kbId || loadingDetails.has(pageId)) return;
  loadingDetails.add(pageId);
  renderShell();
  try {
    const res = await backendApi.wikiPage(pageId, view.kbId);
    view.pageDetails = { ...view.pageDetails, [pageId]: res.page };
    view.sourcePages = { ...view.sourcePages, [pageId]: res.source_pages ?? [] };
    view.relationPages = { ...view.relationPages, [pageId]: res.relation_pages ?? [] };
    view.sourceTitles = { ...view.sourceTitles, ...(res.source_titles || {}) };
  } catch (err) {
    notify(`加载页面详情失败：${errMsg(err)}`);
  } finally {
    loadingDetails.delete(pageId);
  }
  renderShell();
}

/**
 * 选中页面并展示详情（列表条目 / 树节点 / 相关页链接 / 图谱节点共用）。
 * 树视图按需展开祖先目录；本地无完整正文时直接进加载态拉取（brief 条目无正文；
 * 图谱节点可能不在已分页加载的列表里），避免先闪一帧空正文再切加载文案。
 */
function selectWikiPage(pageId: string, opts?: { expandTree?: boolean }): void {
  if (pageId === view.selectedId) return;
  const previousId = view.selectedId;
  if (previousId && detailEditorHandle && detailDirty) void saveWikiPageDraft(previousId);
  view.selectedId = pageId;
  if (opts?.expandTree && view.view === 'tree') {
    const page = view.pages.find((p) => p.id === pageId);
    if (page) {
      for (const p of ancestorPaths(page.file_path)) view.expandedPaths.add(p);
    }
  }
  const needsDetail = !view.pageDetails[pageId] && !view.pages.find((p) => p.id === pageId)?.content;
  // loadPageDetail 同步置加载态后自行渲染「加载中」；上一轮点击的拉取仍在途时直接渲染（加载态由 loadingDetails 派生）。
  if (needsDetail && !loadingDetails.has(pageId)) {
    void loadPageDetail(pageId);
    return;
  }
  renderShell();
}

async function loadVaultDocument(name: 'Home.md' | 'index.md'): Promise<void> {
  if (!view.kbId) return;
  const seq = loadSeq;
  view.selectedId = null;
  view.selectedDocumentName = name;
  view.vaultDocument = null;
  view.detailLoading = true;
  renderShell();
  try {
    const res = await backendApi.wikiVaultDocument(name, view.kbId);
    if (seq !== loadSeq || view.selectedDocumentName !== name) return;
    view.vaultDocument = res.document;
  } catch (err) {
    if (seq === loadSeq) {
      notify(`加载 ${name} 失败：${(err as Error).message}`);
      view.selectedDocumentName = null;
    }
  } finally {
    if (seq === loadSeq) view.detailLoading = false;
  }
  renderShell();
}

/** KB 概览只用于详情空态展示，失败静默（不打扰主流程）。 */
async function loadKbSummary(): Promise<void> {
  if (!view.kbId) return;
  const seq = loadSeq;
  try {
    const res = await backendApi.wikiSummary(view.kbId);
    if (seq !== loadSeq) return;
    view.kbSummary = res.status === 'ready' && res.summary ? { summary: res.summary, page_count: res.page_count, source_count: res.source_count, generated_at: res.generated_at, status: res.status } : null;
    if (!view.selectedId) renderShell();
  } catch {
    /* 概览加载失败不提示 */
  }
}

/** 切 KB / 整页重载（KB 切换、删除、wiki:changed 事件等入口；无页头刷新按钮）。 */
async function reloadAll(): Promise<void> {
  loadSeq += 1;
  view.pages = [];
  view.pageOffset = 0;
  view.hasMore = false;
  view.selectedId = null;
  view.selectedDocumentName = null;
  view.vaultDocument = null;
  view.detailLoading = false;
  view.pageDetails = {};
  view.sourcePages = {};
  view.relationPages = {};
  view.kbSummary = null;
  view.expandedPaths = new Set<string>(DEFAULT_EXPANDED_PATHS);
  loadingDetails.clear();
  // 图谱数据同属本 KB，整页重载一并失效（下次 mount 重新拉取）。
  invalidateWikiGraph();
  view.loading = true;
  renderShell();
  await loadKbs();
  await loadPages();
  if (view.kbId) {
    await loadVaultDocument('Home.md');
  }
  void loadKbSummary();
}

// 文件附件统一从右栏 Composer 进入 Wiki Agent 工作流；页面不再编排上传或 ingest。
// ── 新建 KB（对齐 web WikiHub：kb_id 限英文/数字/下划线，name 缺省同 id） ──

// kb_id 即磁盘目录名（crew/wiki/store/_filesystem.py），后端 normalize_kb_id 只 trim 不限字符集，
// 真实约束 = 合法目录名：沿用后端 filename_from_title 的非法字符集（支持中文）。
// eslint-disable-next-line no-control-regex -- 控制字符段刻意对齐后端 filename_from_title 的目录名清洗规则。
const KB_ID_ILLEGAL_RE = /[\\/:*?"<>|\x00-\x1f\x7f]/;

/** 返回错误文案；合法返回 null。 */
function kbIdError(id: string): string | null {
  if (!id) return '请输入知识库 ID';
  if (id === '.' || id === '..') return '知识库 ID 不能是 . 或 ..';
  if (KB_ID_ILLEGAL_RE.test(id)) return '知识库 ID 不能包含 \\ / : * ? " < > | 字符';
  return null;
}

async function handleCreateKbSubmit(): Promise<void> {
  if (kbCreateSubmitting) return;
  const id = kbCreateDraft.trim();
  const idError = kbIdError(id);
  if (idError) {
    notify(idError);
    return;
  }
  kbCreateSubmitting = true;
  renderShell();
  try {
    const res = await backendApi.wikiCreateKB({ kb_id: id, name: id });
    initializedKbIds.add(id);
    notify(`已创建知识库「${res.kb?.name || id}」`);
    kbCreateOpen = false;
    kbCreateDraft = '';
    // 预选新 KB：loadKbs 会保留仍存在的当前选中项。
    view.kbId = id;
    await reloadAll();
  } catch (err) {
    notify(`新建知识库失败：${errMsg(err)}`);
  } finally {
    kbCreateSubmitting = false;
    renderShell();
  }
}

/** 删除当前选中的普通 KB；内置知识库由前后端共同保护。 */
async function handleDeleteKb(): Promise<void> {
  const kbId = view.kbId;
  if (!kbId || kbId === DEFAULT_KB_ID || kbId === TUTORIAL_KB_ID) return;
  const kbName = view.kbs.find((k) => k.id === kbId)?.name || kbId;
  const confirmed = await showConfirmDialog({
    title: '删除知识库',
    message: `确定删除知识库「${kbName}」吗？其中的全部页面、原始素材和专属 Wiki 问答历史都会永久删除，此操作不可恢复。`,
    confirmText: '删除',
  });
  if (!confirmed) return;
  try {
    await backendApi.wikiDeleteKB(kbId);
    initializedKbIds.delete(kbId);
    wikiAgentKbDeletedHandler?.(kbId);
    notify(`已删除知识库「${kbName}」`);
    await reloadAll();
  } catch (err) {
    notify(`删除知识库失败：${errMsg(err)}`);
  }
}

// ── 删除（Phase 2） ──

/** 删除后重新加载列表与概览（互不依赖，并行）；删的是当前选中页时清空详情栏。 */
async function refreshAfterDelete(deletedIds: string[]): Promise<void> {
  if (view.selectedId && deletedIds.includes(view.selectedId)) {
    view.selectedId = null;
  }
  await Promise.all([loadPages(), loadKbSummary()]);
}

async function handleDeletePage(pageId: string, title: string): Promise<void> {
  const confirmed = await showConfirmDialog({
    title: '删除页面',
    message: `确定删除页面「${title}」？`,
    confirmText: '删除',
  });
  if (!confirmed) return;
  try {
    await backendApi.wikiDeletePage(pageId, view.kbId ?? undefined);
    notify('已删除页面');
    await refreshAfterDelete([pageId]);
  } catch (err) {
    notify(`删除失败：${errMsg(err)}`);
  }
}

async function handleBulkDelete(): Promise<void> {
  const ids = Array.from(view.selectedIds);
  if (ids.length === 0) return;
  const confirmed = await showConfirmDialog({
    title: '批量删除页面',
    message: `确定删除选中的 ${ids.length} 个页面？`,
    confirmText: '删除',
  });
  if (!confirmed) return;
  try {
    const res = await backendApi.wikiDeletePages(ids, view.kbId ?? undefined);
    const failedCount = res.failed?.length ?? 0;
    notify(failedCount > 0 ? `已删除 ${res.deleted.length} 个页面，${failedCount} 个失败` : `已删除 ${res.deleted.length} 个页面`);
    view.selectedIds = new Set<string>();
    await refreshAfterDelete(res.deleted);
  } catch (err) {
    notify(`批量删除失败：${errMsg(err)}`);
  }
}

// ── 图谱视图（Phase 3） ──

/** Wiki Agent 引用页面时，在当前 Wiki 中栏打开，而不是跳回主聊天或弹 overlay。 */
export function openWikiPageInHub(pageId: string): boolean {
  const root = $('#wiki-page-root');
  if (!root || state.activeTab !== 'wiki' || !pageId) return false;
  selectWikiPage(pageId);
  return true;
}

// ── 事件绑定 ──

function bindEvents(): void {
  // 选择器必须限定在本页容器内：各 tab 页常驻 DOM（隐藏而非移除），
  // document 级 $$ 会误绑到其他页面的同名 data-* 元素（如 skills 页的 [data-refresh]）。
  const root = $('#wiki-page-root');
  if (!root) return;
  const $$w = <T extends Element = HTMLElement>(selector: string): T[] =>
    Array.from(root.querySelectorAll(selector)) as T[];

  /** click 绑定简写：给本页容器内所有匹配元素挂同一 handler（收敛下面大量 click 绑定样板）。 */
  const onClick = (selector: string, handler: (el: HTMLElement, ev: MouseEvent) => void): void => {
    $$w(selector).forEach((el) => {
      el.addEventListener('click', (ev) => handler(el, ev));
    });
  };

  const kbSelect = $('#wiki-kb-select') as HTMLSelectElement | null;
  kbSelect?.addEventListener('change', () => {
    const id = kbSelect.value;
    if (!id || id === view.kbId) return;
    view.kbId = id;
    void reloadAll();
  });

  onClick('[data-wiki-view]', (btn) => {
    const next = (btn.getAttribute('data-wiki-view') as WikiListView) || 'timeline';
    if (next === view.view) return;
    view.view = next;
    renderShell();
  });

  onClick('[data-upload]', () => {
    fireWikiAgentEntry(undefined, true);
  });

  // ── 新建 KB 内联表单 ──
  onClick('[data-kb-delete]', () => void handleDeleteKb());

  // ── 批量选择模式 ──
  onClick('[data-batch-toggle]', () => {
    view.selecting = true;
    view.selectedIds = new Set<string>();
    renderShell();
  });
  onClick('[data-batch-done]', () => {
    view.selecting = false;
    view.selectedIds = new Set<string>();
    renderShell();
  });
  onClick('[data-select-all]', () => {
    view.selectedIds = new Set(view.pages.map((p) => p.id));
    renderShell();
  });
  onClick('[data-deselect-all]', () => {
    view.selectedIds = new Set<string>();
    renderShell();
  });
  onClick('[data-bulk-delete]', () => void handleBulkDelete());

  onClick('[data-delete-id]', (btn, e) => {
    // 删除按钮在条目内部，阻止冒泡避免触发选中/打开详情。
    e.stopPropagation();
    const id = btn.getAttribute('data-delete-id') ?? '';
    if (!id) return;
    void handleDeletePage(id, btn.getAttribute('data-delete-title') ?? '');
  });
  onClick('[data-kb-create-toggle]', () => {
    kbCreateOpen = !kbCreateOpen;
    renderShell();
    if (kbCreateOpen) {
      root.querySelector<HTMLInputElement>('[data-kb-create-input]')?.focus();
    }
  });
  onClick('[data-wiki-tour]', () => startWikiTour());
  onClick('[data-kb-create-cancel]', () => {
    kbCreateOpen = false;
    kbCreateDraft = '';
    renderShell();
  });
  onClick('[data-kb-create-submit]', () => void handleCreateKbSubmit());
  $$w('[data-kb-create-input]').forEach((input) => {
    // input 事件只记草稿不重渲染（避免每次击键重建 DOM 丢焦点）。
    input.addEventListener('input', () => {
      kbCreateDraft = (input as HTMLInputElement).value;
    });
    input.addEventListener('keydown', (e) => {
      const ke = e as KeyboardEvent;
      if (ke.key === 'Enter') void handleCreateKbSubmit();
      if (ke.key === 'Escape') {
        kbCreateOpen = false;
        kbCreateDraft = '';
        renderShell();
      }
    });
  });

  // ── 左栏分栏把手：列表/树/类型视图调 listWidth；图谱视图调 graphWidth（null=弹性分配，拖过后固定像素） ──
  $$w('[data-wiki-sash]').forEach((sash) => {
    const pane = root.querySelector<HTMLElement>('.wiki-list-pane');
    if (!pane) return;
    bindPaneSash(sash, {
      // 图谱模式弹性分配时 listWidth 不反映当前宽度，取实际测量值
      startWidth: () => (view.view === 'graph' ? pane.getBoundingClientRect().width : listWidth),
      onDrag: (w) => {
        if (view.view === 'graph') {
          graphWidth = graphWidthStore.clamp(w);
          // 弹性分配下 width 不生效（flex-basis: 0），需同步切为固定尺寸
          pane.style.flex = '0 0 auto';
          pane.style.width = `${graphWidth}px`;
        } else {
          listWidth = listWidthStore.clamp(w);
          pane.style.width = `${listWidth}px`;
        }
      },
      onCommit: () => (view.view === 'graph' ? graphWidthStore.persist(graphWidth) : listWidthStore.persist(listWidth)),
      // 双击复位：列表回默认宽度；图谱回 1.5:1 弹性分配
      onReset: () => {
        if (view.view === 'graph') {
          graphWidth = null;
          graphWidthStore.persist(null);
        } else {
          listWidth = WIKI_LIST_DEFAULT_WIDTH;
          listWidthStore.persist(listWidth);
        }
        renderShell();
      },
    });
  });

  onClick('[data-load-more]', () => void loadMorePages());

  onClick('[data-page-id]', (item) => {
    const id = item.getAttribute('data-page-id');
    if (!id) return;
    // 批量选择模式：点击条目切换选中而非打开详情。
    if (view.selecting) {
      if (view.selectedIds.has(id)) view.selectedIds.delete(id);
      else view.selectedIds.add(id);
      renderShell();
      return;
    }
    // 从 vault 文档（Home.md/index.md）切回页面时清掉文档态，否则 detailHtml 仍优先显示文档。
    view.selectedDocumentName = null;
    view.vaultDocument = null;
    selectWikiPage(id, { expandTree: true });
  });

  $$w<HTMLInputElement>('[data-wiki-title]').forEach((input) => {
    input.addEventListener('input', () => {
      if (view.selectedId) scheduleWikiPageSave(view.selectedId);
    });
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        input.blur();
      }
    });
  });
  onClick('[data-source-page-id]', (pill) => {
    const pageId = pill.getAttribute('data-source-page-id');
    if (!pageId) return;
    view.selectedDocumentName = null;
    view.vaultDocument = null;
    selectWikiPage(pageId, { expandTree: true });
  });
  onClick('[data-related-page-id]', (item) => {
    const pageId = item.getAttribute('data-related-page-id');
    if (!pageId) return;
    view.selectedDocumentName = null;
    view.vaultDocument = null;
    selectWikiPage(pageId, { expandTree: true });
  });

  onClick('[data-vault-document]', (item) => {
    const name = item.getAttribute('data-vault-document');
    if (name !== 'Home.md' && name !== 'index.md') return;
    if (name === view.selectedDocumentName && view.vaultDocument) return;
    void loadVaultDocument(name);
  });

  onClick('[data-tree-path]', (btn) => {
    const path = btn.getAttribute('data-tree-path') ?? '';
    if (view.expandedPaths.has(path)) view.expandedPaths.delete(path);
    else view.expandedPaths.add(path);
    renderShell();
  });

  // 事件委托：markdown 正文中的 [[名称]] 按钮与 Home.md 推荐问题按钮在
  // mountFoldedMarkdown 之后才加入 DOM，直接绑定会漏掉，改用委托捕获。
  if (root.dataset.wikiRelBound !== 'true') {
    root.dataset.wikiRelBound = 'true';
    root.addEventListener('click', (e) => {
      const askBtn = (e.target as HTMLElement).closest('[data-wiki-ask]') as HTMLElement | null;
      if (askBtn) {
        fireWikiAgentPrompt(askBtn.getAttribute('data-wiki-ask') ?? '');
        return;
      }
      const btn = (e.target as HTMLElement).closest('[data-rel-title]') as HTMLElement | null;
      if (!btn) return;
      const title = btn.getAttribute('data-rel-title') ?? '';
      void resolveAndOpenWikiPage(title);
    });
  }
}

// ── 对外入口 ──

export function renderWikiPage(): void {
  renderShell();
}

/** 切入 Wiki tab 时调用：首次加载（加载失败不置 loaded，下次切入自动重试）。 */
export async function refreshWikiData(): Promise<void> {
  if (view.loaded) {
    renderShell();
    return;
  }
  await reloadAll();
  // 仅首次加载成功才置位：失败（如网络异常）时保持 false，下次切回 tab 自动重试。
  if (!view.kbsLoadFailed) {
    view.loaded = true;
    // 首次成功进入 Wiki 页时启动界面导览（localStorage 标记，只启动一次）。
    maybeStartWikiTourOnce();
  }
}

export function bindWikiTab(onTab: () => void): void {
  document.querySelector('[data-tab="wiki"]')?.addEventListener('click', () => {
    onTab();
    void refreshWikiData();
  });

  // 登录态变化（登录成功 / 退出）后重置缓存，下次进入 tab 重新拉取。
  window.addEventListener('user:login-changed', () => {
    resetWikiViewState();
    invalidateWikiGraph();
    renderShell();
    // 登录成功且当前停在 Wiki tab：立即自动拉取（登出则停在登录引导态）。
    if (state.activeTab === 'wiki') {
      void refreshWikiData();
    }
  });

  if (!wikiChangedBound) {
    wikiChangedBound = true;
    window.addEventListener('wiki:changed', (event) => {
      const changes = ((event as CustomEvent<{ changes?: Array<{ kb_id?: string }> }>).detail?.changes ?? []);
      if (!view.kbId || !changes.some((change) => change.kb_id === view.kbId)) return;
      if (localSaveInFlight || Date.now() < ignoreWikiChangedUntil) return;
      invalidateWikiGraph();
      void reloadAll();
    });
  }
}
