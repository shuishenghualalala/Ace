/**
 * Wiki 知识库页：知识浏览、直接新建/编辑，以及右栏 Wiki Agent 对话。
 *
 * 数据源：GET /api/wiki/kbs + /api/wiki/pages（brief=1 分页）+ /api/wiki/pages/{id} + /api/wiki/summary
 *         + /api/wiki/graph（Phase 3 图谱，由 features/wiki-graph.ts 消费）
 * 写操作：POST /api/wiki/upload（主进程 gateway:upload IPC）+ /api/wiki/ingest(+cancel)
 *         + DELETE /api/wiki/pages/{id} + DELETE /api/wiki/pages（批量）
 *         + POST /api/wiki/kbs（新建 KB，内联表单）+ POST /api/wiki/init（无 KB 自动初始化）
 *         + DELETE /api/wiki/kbs/{id}（删除 KB，default 不可删）
 *
 * 布局：
 *   1. 页头：KB 选择器（下拉）+ 新建 KB + 新建页面 + 上传 + 批量管理 + 刷新（「问 Wiki」已下线：
 *      右栏对话面板常驻，无需入口按钮）
 *   2. 上传任务面板：每个 source 的进度条 + 阶段文案 + 错误态；进度经 WS
 *      wiki_ingest_progress 帧（chat-controller 回调转发）实时更新
 *   3. 左栏列表：分页「加载更多」；条目 = 标题 + 类型徽标 + 更新时间 + 摘要；
 *      单条删除按钮；批量管理模式下条目变 checkbox 选择；
 *      「图谱」视图下左栏替换为图谱画布（features/wiki-graph.ts，本文件只注入容器 + 回调）
 *   3.5 分栏把手：列表 | 详情 | 对话之间可拖拽调宽（localStorage 持久化，双击复位；图谱模式无列表把手）
 *   4. 右栏详情：标题 + 元信息 + Markdown 正文；未选中时显示 KB 概览 / 空态
 *   5. 最右栏：Wiki Agent 对话面板（features/wiki-agent.ts 挂载，常驻）
 *
 * 边界态：未登录显示登录引导态（不发请求）；没有任何 KB 时自动初始化 default
 * （对齐 web WikiHub，后端幂等，只自动试一次，失败靠「刷新」重试）。
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
  type WikiSourceFiles,
  type WikiSourcePage,
  type WikiSourceTitles,
  type WikiSummary,
  type WikiVaultDocument,
} from '../backend-client';
import { $, escapeHtml, notify, state } from '../state';
import { clearRuntimeStyle, setRuntimeStyle } from '../components/runtime-style';
import { renderMarkdownHtml } from '../markdown';
import { mountFoldedMarkdown, type FoldedMarkdownHandle } from '../markdown-fold';
import { showConfirmDialog } from '../ui-feedback';
import { isRendererLoggedIn, requireRendererLogin } from './auth-gate';
import { __resetWikiGraphForTest, invalidateWikiGraph, mountWikiGraph } from './wiki-graph';
import {
  createWikiWorkspaceView,
  type WikiWorkspaceView,
} from './wiki-workspace';
import { maybeStartWikiTourOnce, startWikiTour } from './wiki-tour';

// ── Wiki Agent 入口（Phase 4） ──
// 「上传」按钮（打开右栏附件选择）与失败任务「让 AI 处理」共用同一挂点；回调由 index.ts
// 组合根注入（接到 features/wiki-agent.ts 的 enterWikiAgentMode，本文件不 import wiki-agent 内部）。

export interface WikiAgentAssist {
  fileName: string;
  error: string;
  sourceId?: string | null;
}

export interface WikiAgentEntryRequest {
  kbId: string;
  kbName: string;
  /** 反向注入：上传/编译失败上下文，存在时进入 Wiki 模式后自动发送挽救 prompt。 */
  assist?: WikiAgentAssist;
  /** 直接发往 Wiki Agent 的提问（Home.md 推荐问题点击）。 */
  prompt?: string;
  /** 聚焦右栏并打开标准 Composer 附件选择。 */
  openAttachment?: boolean;
}

let wikiAgentEntryHandler: ((req: WikiAgentEntryRequest) => void) | null = null;
let wikiAgentPanelRenderer: ((root: HTMLElement, req: WikiAgentEntryRequest) => void) | null = null;
let wikiWorkspaceView: WikiWorkspaceView | null = null;
let detailFoldHandle: FoldedMarkdownHandle | null = null;
let detailRenderKey = '';
let wikiAgentKbDeletedHandler: ((kbId: string) => void) | null = null;

export function setWikiAgentEntryHandler(fn: ((req: WikiAgentEntryRequest) => void) | null): void {
  wikiAgentEntryHandler = fn;
}

export function setWikiAgentPanelRenderer(
  fn: ((root: HTMLElement, req: WikiAgentEntryRequest) => void) | null,
): void {
  wikiAgentPanelRenderer = fn;
}

export function setWikiAgentKbDeletedHandler(fn: ((kbId: string) => void) | null): void {
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

/** Home.md 推荐问题点击：把问题直接发给 Wiki Agent。 */
function fireWikiAgentPrompt(prompt: string): void {
  const text = prompt.trim();
  if (!view.kbId || !text) return;
  wikiAgentEntryHandler?.({ kbId: view.kbId, kbName: currentKbName(), prompt: text });
}

/** 列表视图：时间线 / 文件树 / 类型 / 图谱（对齐 web 端 WikiViewMode）。 */
export type WikiListView = 'timeline' | 'tree' | 'type' | 'graph';

const PAGE_LIMIT = 200;
const DEFAULT_KB_ID = 'default';
/** 后端预置的教程知识库 id（不可删除）。 */
const TUTORIAL_KB_ID = 'tutorial';
const DEFAULT_EXPANDED_PATHS = ['wiki', 'wiki/sources'] as const;

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

export const TYPE_META: Record<WikiPageType, { order: number; label: string; shortLabel: string }> = {
  entity: { order: 0, label: '关键词', shortLabel: '关键词' },
  concept: { order: 0, label: '概念', shortLabel: '概念' },
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

const VAULT_PAGE_DIRS = new Set(['entities', 'topics', 'sources', 'comparisons', 'synthesis']);

/**
 * 归一化 Vault 相对路径：旧版种子/历史页面的 file_path 可能没有 wiki/ 前缀
 * （如 entities/xxx.md），统一补成 wiki/ 前缀，保证文件树能识别。
 */
export function normalizeVaultPath(filePath: string): string {
  const parts = (filePath || '')
    .split('/')
    .map((s) => s.trim())
    .filter(Boolean);
  if (parts.length > 0 && parts[0] !== 'wiki' && VAULT_PAGE_DIRS.has(parts[0])) {
    return ['wiki', ...parts].join('/');
  }
  return parts.join('/');
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
    const parts = normalizeVaultPath(page.file_path).split('/').filter(Boolean);
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
      const nameA = a.kind === 'document' ? a.name : a.kind === 'page' ? a.page.title : a.name;
      const nameB = b.kind === 'document' ? b.name : b.kind === 'page' ? b.page.title : b.name;
      return nameA.localeCompare(nameB, 'zh-CN');
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
  const parts = normalizeVaultPath(filePath).split('/').filter(Boolean);
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
  const types = (Object.keys(TYPE_META) as WikiPageType[])
    .sort((a, b) => TYPE_META[a].order - TYPE_META[b].order);
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
  /** 已加载完整正文的页面（pageId → WikiPage）；列表接口只返回 brief。 */
  pageDetails: Record<string, WikiPage>;
  detailLoading: boolean;
  /** 页面详情接口返回的来源摘要页（pageId -> 可跳转来源）。 */
  sourcePages: Record<string, WikiSourcePage[]>;
  /** 页面详情接口返回的正向与反向结构化关系。 */
  relationPages: Record<string, WikiRelationPage[]>;
  sourceTitles: WikiSourceTitles;
  sourceFiles: WikiSourceFiles;
  /** KB 概览（/api/wiki/summary）；未生成或加载失败时为 null。 */
  kbSummary: Omit<WikiSummary, 'kb_id'> | null;
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
    pageDetails: {},
    detailLoading: false,
    sourcePages: {},
    relationPages: {},
    sourceTitles: {},
    sourceFiles: {},
    kbSummary: null,
    expandedPaths: new Set<string>(DEFAULT_EXPANDED_PATHS),
    loaded: false,
    kbsLoadFailed: false,
    selecting: false,
    selectedIds: new Set<string>(),
  };
}

let view: WikiViewState = initialViewState();

/** 加载代际：切 KB / 刷新后作废旧请求的回包，避免 stale 数据覆盖新列表。 */
let loadSeq = 0;
/** 本次页面生命周期内已补齐 Vault 骨架的 KB；wikiInit 本身是幂等操作。 */
const initializedKbIds = new Set<string>();
/** 「无 KB 时自动初始化 default」是否已尝试过（对齐 web WikiHub：只自动试一次，失败靠手动刷新）。 */
let autoInitAttempted = false;
/** 新建 KB 内联表单状态（renderShell 重建 DOM 时草稿不丢）。 */
let kbCreateOpen = false;
let kbCreateDraft = '';
let kbCreateSubmitting = false;
type WikiPageEditorMode = 'create' | 'edit' | null;
let pageEditorMode: WikiPageEditorMode = null;
let pageEditorSubmitting = false;

// ── 列表栏宽度：可拖拽 + 持久化（对齐 web ResizablePanels 分栏拖拽，实现模式同 inspector.ts） ──
const WIKI_LIST_WIDTH_KEY = 'crew.desktop.wikiListWidth.v1';
const WIKI_LIST_DEFAULT_WIDTH = 340;
const WIKI_LIST_MIN_WIDTH = 240;
const WIKI_LIST_MAX_WIDTH = 640;

function clampListWidth(w: number): number {
  if (!Number.isFinite(w)) return WIKI_LIST_DEFAULT_WIDTH;
  const vwCap = Math.max(WIKI_LIST_MIN_WIDTH, Math.floor(window.innerWidth * 0.5));
  return Math.max(WIKI_LIST_MIN_WIDTH, Math.min(Math.min(WIKI_LIST_MAX_WIDTH, vwCap), Math.round(w)));
}

function loadListWidth(): number {
  try {
    const raw = localStorage.getItem(WIKI_LIST_WIDTH_KEY);
    if (!raw) return WIKI_LIST_DEFAULT_WIDTH;
    return clampListWidth(parseInt(raw, 10));
  } catch {
    return WIKI_LIST_DEFAULT_WIDTH;
  }
}

function persistListWidth(): void {
  try {
    localStorage.setItem(WIKI_LIST_WIDTH_KEY, String(listWidth));
  } catch {
    /* quota / disabled */
  }
}

let listWidth = loadListWidth();
/** 正在加载详情的 pageId（防重复点击重复请求）。 */
const loadingDetails = new Set<string>();

/** 测试钩子：覆盖 view 状态（单测用）。 */
export function __setWikiViewForTest(patch: Partial<WikiViewState>): void {
  view = { ...view, ...patch };
}

/** 测试钩子：重置为初始状态（单测用）。 */
export function __resetWikiViewForTest(): void {
  detailFoldHandle?.dispose();
  detailFoldHandle = null;
  detailRenderKey = '';
  view = initialViewState();
  wikiWorkspaceView = null;
  loadSeq = 0;
  initializedKbIds.clear();
  autoInitAttempted = false;
  kbCreateOpen = false;
  kbCreateDraft = '';
  kbCreateSubmitting = false;
  pageEditorMode = null;
  pageEditorSubmitting = false;
  loadingDetails.clear();
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
            <button type="button" class="wiki-tree__label" style="padding-left: ${treeIndentPx(depth)}px">
              <span class="wiki-tree__title">${escapeHtml(vaultDocumentLabel(node.name))}</span>
            </button>
          </li>`;
      }
      const active = node.page.id === view.selectedId ? ' is-active' : '';
      const { checkedClass, checkHtml } = selectionMark(node.page.id);
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
  if (view.pages.length === 0) return '';
  if (view.view === 'tree') return fileTreeViewHtml();
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
  sourceTitles?: WikiSourceTitles;
  sourcePages?: WikiSourcePage[];
  relationPages?: WikiRelationPage[];
  /** 用于把「相关页面」标题解析为可点击链接的页面列表；缺省时相关页只渲染 [[标题]] 文本。 */
  pages?: WikiPage[];
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
  const tags = page.tags.length
    ? `<div class="wiki-detail__tags">${page.tags.map((t) => `<span class="wiki-item__tag">${escapeHtml(t)}</span>`).join('')}</div>`
    : '';
  const content = page.content
    ? '<div class="md-body chat-markdown wiki-detail__content" data-wiki-fold-content></div>'
    : `<div class="wiki-detail__no-content">（暂无正文）</div>`;
  // 来源：优先用结构化 sourcePages（可跳转），回退到 source_id + sourceTitles 文本
  const sourcePills = opts.sourcePages?.length
    ? opts.sourcePages.map((target) => `
        <button type="button" class="wiki-badge wiki-source-pill"
          data-source-page-id="${escapeHtml(target.id)}">${escapeHtml(target.title)}</button>`).join('')
    : page.sources.length > 0
      ? `<ul>${page.sources
          .map((src) => `<li title="source_id: ${escapeHtml(src)}">${escapeHtml(opts.sourceTitles?.[src] || src)}</li>`)
          .join('')}</ul>`
      : '';
  const sources = sourcePills
    ? `<div class="wiki-detail__section">
          <h4>来源</h4>
          ${sourcePills.startsWith('<ul>') ? sourcePills : `<div class="wiki-source-pills">${sourcePills}</div>`}
        </div>`
    : '';
  // 相关页面：优先用结构化 relationPages（双链），回退到 related 标题列表
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
    : '';
  const related = (visibleRelations.length || page.related.length > 0)
    ? `<div class="wiki-detail__section">
          <h4>相关页面</h4>
          ${relationCards
            ? `<div class="wiki-related-pages__grid">${relationCards}</div>`
            : `<ul>${page.related
                .map((rel) => {
                  const target = opts.pages?.find((p) => p.title === rel);
                  return `<li>${target ? `<button type="button" class="wiki-detail__rel-link" data-rel-title="${escapeHtml(rel)}">[[${escapeHtml(rel)}]]</button>` : `[[${escapeHtml(rel)}]]`}</li>`;
                })
                .join('')}</ul>`}
        </div>`
    : '';
  return `
    <article class="wiki-detail">
      <header class="wiki-detail__header">
        <div class="wiki-detail__badges">
          ${typeBadge(page.page_type, true)}
        </div>
        <button type="button" class="hub-refresh-btn wiki-detail__edit" data-page-edit="${escapeHtml(page.id)}">编辑</button>
        <h2 class="wiki-detail__title">${escapeHtml(page.title)}</h2>
        <div class="wiki-detail__meta">
          <span>更新于 ${escapeHtml(formatWikiTime(page.updated_at))}</span>
          <span class="wiki-detail__path" title="文件路径">${escapeHtml(page.file_path || '')}</span>
        </div>
        ${tags}
      </header>
      ${content}
      ${sources}
      ${related}
    </article>`;
}

/** 将 Wiki 详情正文挂载为渐进 Markdown，避免长文档同步阻塞渲染线程。 */
export function mountWikiDetailFold(
  root: ParentNode,
  page: WikiPage | null | undefined,
): FoldedMarkdownHandle | null {
  const target = root.querySelector<HTMLElement>('[data-wiki-fold-content]');
  if (!target || !page?.content) return null;
  return mountFoldedMarkdown(target, page.content);
}

function pageEditorHtml(): string {
  const editing = pageEditorMode === 'edit';
  const page = editing && view.selectedId
    ? view.pageDetails[view.selectedId] ?? view.pages.find((item) => item.id === view.selectedId)
    : null;
  const pageType = page?.page_type ?? 'topic';
  return `
    <form class="wiki-page-editor" data-page-editor>
      <header class="wiki-page-editor__header">
        <h2>${editing ? '编辑 Wiki 页面' : '新建 Wiki 页面'}</h2>
        <div class="wiki-page-editor__actions">
          <button type="button" class="hub-refresh-btn" data-page-editor-cancel>取消</button>
          <button type="submit" class="hub-refresh-btn"${pageEditorSubmitting ? ' disabled' : ''}>${pageEditorSubmitting ? '保存中…' : '保存'}</button>
        </div>
      </header>
      <label class="wiki-page-editor__field">标题
        <input name="title" required maxlength="200" value="${escapeHtml(page?.title ?? '')}" />
      </label>
      <div class="wiki-page-editor__row">
        <label class="wiki-page-editor__field">类型
          <select name="page_type"${editing ? ' disabled' : ''}>
            ${(['topic', 'concept', 'entity', 'source'] as WikiPageType[]).map((type) => `<option value="${type}"${type === pageType ? ' selected' : ''}>${TYPE_META[type].label}</option>`).join('')}
          </select>
        </label>
        <label class="wiki-page-editor__field">状态
          <select name="status">
            <option value="published"${page?.status !== 'deprecated' ? ' selected' : ''}>已发布</option>
            <option value="deprecated"${page?.status === 'deprecated' ? ' selected' : ''}>已废弃</option>
          </select>
        </label>
      </div>
      <label class="wiki-page-editor__field wiki-page-editor__field--content">正文（Markdown）
        <textarea name="content" spellcheck="false">${escapeHtml(page?.content ?? '')}</textarea>
      </label>
    </form>`;
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
  if (pageEditorMode) return pageEditorHtml();
  if (view.selectedDocumentName) {
    if (view.detailLoading || !view.vaultDocument) {
      return '<div class="wiki-detail__empty"><p class="wiki-detail__empty-hint">加载文档中…</p></div>';
    }
    return `<article class="wiki-detail${view.selectedDocumentName === 'Home.md' ? ' wiki-home-document' : ''}"><header class="wiki-detail__header">
      <div class="wiki-detail__badges"><span class="wiki-badge">${view.selectedDocumentName === 'Home.md' ? '概览' : '文件'}</span></div>
      <h2 class="wiki-detail__title">${escapeHtml(vaultDocumentLabel(view.selectedDocumentName))}</h2>
      <div class="wiki-detail__meta"><span>更新于 ${escapeHtml(formatWikiTime(view.vaultDocument.updated_at))}</span></div>
    </header><div class="md-body chat-markdown wiki-detail__content" data-wiki-fold-content></div></article>`;
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
  if (view.detailLoading && !view.pageDetails[view.selectedId]) {
    return `<div class="wiki-detail__empty"><p class="wiki-detail__empty-hint">加载页面详情中…</p></div>`;
  }
  const page = view.pageDetails[view.selectedId] ?? view.pages.find((p) => p.id === view.selectedId);
  if (!page) {
    return `<div class="wiki-detail__empty"><p class="wiki-detail__empty-hint">选择左侧页面查看详情</p></div>`;
  }
  return wikiDetailArticleHtml(page, {
    sourceTitles: view.sourceTitles,
    sourcePages: view.sourcePages[page.id],
    relationPages: view.relationPages[page.id],
    pages: view.pages,
  });
}

function wikiEmptyStateHtml(): string {
  if (!isRendererLoggedIn()) {
    return `
      <div class="v2-empty">
        <div class="v2-empty__title">登录后使用 Wiki 知识库</div>
        <div class="v2-empty__desc">登录后可浏览知识库页面、上传文件编译，并向 Wiki Agent 提问。</div>
      </div>`;
  }
  const title = view.kbsLoadFailed ? '知识库加载失败' : view.loading ? '知识库加载中…' : '暂无知识库';
  const description = view.kbsLoadFailed
    ? '请检查后端服务连接后，点击「刷新」重试。'
    : view.loading
      ? '正在读取知识库列表。'
      : '点击「刷新」重试。';
  return `
    <div class="v2-empty">
      <div class="v2-empty__title">${title}</div>
      <div class="v2-empty__desc">${description}</div>
    </div>`;
}

function ensureWikiWorkspace(root: HTMLElement): WikiWorkspaceView {
  if (!wikiWorkspaceView) {
    wikiWorkspaceView = createWikiWorkspaceView();
    wikiWorkspaceView.element.classList.add('page-shell', 'page-shell--wiki');
    wikiWorkspaceView.slots.navigation.classList.add('wiki-list-pane');
    wikiWorkspaceView.slots.sash.classList.add('wiki-sash');
    wikiWorkspaceView.slots.detail.classList.add('wiki-detail-pane');
    wikiWorkspaceView.slots.agentSash.classList.add('wiki-sash');
    wikiWorkspaceView.slots.agent.classList.add('wiki-agent-pane');
  }
  if (!root.contains(wikiWorkspaceView.element)) root.replaceChildren(wikiWorkspaceView.element);
  return wikiWorkspaceView;
}

function renderWikiHeader(workspace: WikiWorkspaceView): void {
  const kbOptions = view.kbs.length
    ? view.kbs.map((kb) => (
      `<option value="${escapeHtml(kb.id)}"${kb.id === view.kbId ? ' selected' : ''}>${escapeHtml(kb.name)}</option>`
    )).join('')
    : '<option value="" selected>暂无知识库</option>';
  const uploadDisabled = !view.kbId;
  const batchToggle = view.kbId && !view.selecting && view.pages.length > 0 && view.view !== 'graph'
    ? '<button type="button" class="hub-refresh-btn" data-batch-toggle>批量管理</button>'
    : '';
  workspace.slots.header.innerHTML = `
    <div class="page-header page-header--hub">
      <div class="page-header__copy">
        <h1 class="page-header__title">Wiki <span class="accent">知识库</span></h1>
        <p class="page-header__desc">大模型自动整理的本地知识库笔记，按时间线、文件夹、类型或图谱浏览。</p>
      </div>
      <div class="page-header__actions">
        <select id="wiki-kb-select" class="wiki-kb-select" aria-label="选择知识库"${view.kbs.length ? '' : ' disabled'}>${kbOptions}</select>
        <button type="button" class="hub-refresh-btn" data-kb-create-toggle>新建</button>
        <button type="button" class="hub-refresh-btn" data-page-create${view.kbId ? '' : ' disabled'}>新建页面</button>
        <button type="button" class="hub-refresh-btn" data-kb-delete${!view.kbId || view.kbId === DEFAULT_KB_ID || view.kbId === TUTORIAL_KB_ID ? ' disabled' : ''}>删除</button>
        <button type="button" class="hub-refresh-btn" data-upload${uploadDisabled ? ' disabled' : ''}>上传</button>
        ${batchToggle}
        <button type="button" class="hub-refresh-btn hub-refresh-btn--help" data-wiki-tour title="使用导览" aria-label="使用导览">?</button>
        <button type="button" class="hub-refresh-btn wiki-refresh-btn" data-refresh title="刷新" aria-label="刷新">
          <svg class="mw-icon" viewBox="0 0 24 24" aria-hidden="true"><use href="#icon-refresh"></use></svg>
        </button>
      </div>
    </div>`;
  workspace.slots.notice.innerHTML = kbCreateHtml();
}

function renderWikiNavigation(workspace: WikiWorkspaceView): void {
  const viewTabs: Array<{ key: WikiListView; label: string }> = [
    { key: 'timeline', label: '时间线' },
    { key: 'tree', label: '文件树' },
    { key: 'type', label: '类型' },
    { key: 'graph', label: '图谱' },
  ];
  const tabs = viewTabs.map((tab) => (
    `<button type="button" class="hub-segment__item${view.view === tab.key ? ' is-active' : ''}" data-wiki-view="${tab.key}" aria-pressed="${view.view === tab.key}">${tab.label}</button>`
  )).join('');
  const graphMode = view.view === 'graph';
  const listBody = graphMode
    ? '<div class="wiki-graph-mount" data-graph-mount></div>'
    : view.loading && view.pages.length === 0
      ? '<div class="wiki-list__loading">页面加载中…</div>'
      : view.pages.length === 0 && view.view !== 'tree'
        ? '<div class="wiki-list__empty">该知识库暂无 Wiki 页面。</div>'
        : `${listViewHtml()}${loadMoreHtml()}`;

  workspace.setGraphMode(graphMode);
  workspace.slots.navigation.classList.toggle('wiki-list-pane--graph', graphMode);
  if (graphMode) clearRuntimeStyle(workspace.slots.navigation, 'width');
  else setRuntimeStyle(workspace.slots.navigation, 'width', `${listWidth}px`);
  workspace.slots.navigation.innerHTML = `
    ${batchBarHtml()}
    <nav class="hub-segment wiki-view-tabs" aria-label="列表视图">${tabs}</nav>
    <div class="wiki-list-scroll${graphMode ? ' wiki-list-scroll--graph' : ''}">${listBody}</div>`;
}

function renderShell(): void {
  const root = $('#wiki-page-root');
  if (!root) return;
  const workspace = ensureWikiWorkspace(root);
  renderWikiHeader(workspace);

  if (!isRendererLoggedIn() || !view.kbId) {
    detailFoldHandle?.dispose();
    detailFoldHandle = null;
    detailRenderKey = '';
    workspace.setGraphMode(false);
    workspace.slots.navigation.hidden = true;
    workspace.slots.sash.hidden = true;
    workspace.slots.detail.hidden = false;
    workspace.slots.detail.innerHTML = wikiEmptyStateHtml();
    workspace.slots.agentSash.hidden = true;
    workspace.slots.agent.hidden = true;
    workspace.slots.agent.replaceChildren();
    bindEvents();
    return;
  }

  workspace.slots.navigation.hidden = false;
  workspace.slots.detail.hidden = false;
  workspace.slots.agentSash.hidden = false;
  workspace.slots.agent.hidden = false;
  renderWikiNavigation(workspace);
  const detailPage = view.selectedId
    ? view.pageDetails[view.selectedId] ?? view.pages.find((page) => page.id === view.selectedId)
    : null;
  const nextDetailKey = JSON.stringify([
    pageEditorMode,
    pageEditorSubmitting,
    view.selectedId,
    view.selectedDocumentName,
    view.detailLoading,
    view.kbSummary,
    view.vaultDocument?.updated_at,
    view.vaultDocument?.content,
    detailPage?.updated_at,
    detailPage?.content,
  ]);
  if (nextDetailKey !== detailRenderKey) {
    detailFoldHandle?.dispose();
    workspace.slots.detail.innerHTML = detailHtml();
    if (view.selectedDocumentName && view.vaultDocument?.content) {
      const target = workspace.slots.detail.querySelector<HTMLElement>('[data-wiki-fold-content]');
      detailFoldHandle = target
        ? mountFoldedMarkdown(target, view.vaultDocument.content)
        : null;
      // Home.md 推荐问题后处理：把 h2+ul 替换为可点击的提问按钮（仅 Home.md 有此小节）
      if (detailFoldHandle && view.selectedDocumentName === 'Home.md' && target) {
        decorateHomeQuestions(target);
      }
    } else {
      detailFoldHandle = mountWikiDetailFold(workspace.slots.detail, detailPage);
    }
    detailRenderKey = nextDetailKey;
  }

  if (workspace.slots.agent.dataset.kbId !== view.kbId) {
    workspace.slots.agent.replaceChildren();
    wikiAgentPanelRenderer?.(workspace.slots.agent, {
      kbId: view.kbId,
      kbName: currentKbName(),
    });
    workspace.slots.agent.dataset.kbId = view.kbId;
  }

  if (view.view === 'graph') {
    const mount = workspace.slots.navigation.querySelector<HTMLElement>('[data-graph-mount]');
    if (mount) {
      mountWikiGraph(mount, view.kbId, {
        onSelectPage: handleGraphSelectPage,
        getSelectedId: () => view.selectedId,
      });
    }
  }
  bindEvents();
}

// ── 数据加载 ──

async function loadKbs(): Promise<void> {
  const seq = ++loadSeq;
  try {
    let res = await backendApi.wikiKBs();
    if (seq !== loadSeq) return;
    // 对齐 web WikiHub：没有任何知识库时自动初始化 default（后端幂等，只自动试一次，
    // 失败落入失败空态，由「刷新」手动重试）。
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
        notify(`初始化默认知识库失败：${(initErr as Error).message}`);
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
    view.sourceFiles = res.source_files || {};
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
    view.sourceFiles = { ...view.sourceFiles, ...(res.source_files || {}) };
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
  view.detailLoading = true;
  renderShell();
  try {
    const res = await backendApi.wikiPage(pageId, view.kbId);
    view.pageDetails = { ...view.pageDetails, [pageId]: res.page };
    view.sourceTitles = { ...view.sourceTitles, ...(res.source_titles || {}) };
    view.sourceFiles = { ...view.sourceFiles, ...(res.source_files || {}) };
    view.sourcePages = {
      ...view.sourcePages,
      [pageId]: (res as { source_pages?: WikiSourcePage[] }).source_pages ?? view.sourcePages[pageId] ?? [],
    };
    view.relationPages = {
      ...view.relationPages,
      [pageId]: (res as { relation_pages?: WikiRelationPage[] }).relation_pages ?? view.relationPages[pageId] ?? [],
    };
  } catch (err) {
    notify(`加载页面详情失败：${errMsg(err)}`);
  } finally {
    loadingDetails.delete(pageId);
    view.detailLoading = loadingDetails.size > 0;
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
    view.kbSummary = res.status === 'ready' && res.summary ? res : null;
    if (!view.selectedId) renderShell();
  } catch {
    /* 概览加载失败不提示 */
  }
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
      notify(`加载知识库文档失败：${errMsg(err)}`);
      view.selectedDocumentName = null;
    }
  } finally {
    if (seq === loadSeq) {
      view.detailLoading = false;
      renderShell();
    }
  }
}

/** 切 KB / 刷新时的整页重载。 */
async function reloadAll(): Promise<void> {
  loadSeq += 1;
  view.pages = [];
  view.pageOffset = 0;
  view.hasMore = false;
  view.selectedId = null;
  view.selectedDocumentName = null;
  view.vaultDocument = null;
  view.pageDetails = {};
  view.detailLoading = false;
  view.sourcePages = {};
  view.relationPages = {};
  view.kbSummary = null;
  view.expandedPaths = new Set<string>(DEFAULT_EXPANDED_PATHS);
  pageEditorMode = null;
  loadingDetails.clear();
  // 图谱数据同属本 KB，整页刷新一并失效（下次 mount 重新拉取）。
  invalidateWikiGraph();
  view.loading = true;
  renderShell();
  await loadKbs();
  await loadPages();
  if (view.kbId) void loadVaultDocument('Home.md');
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
  if (!requireRendererLogin('请先登录后再新建知识库')) return;
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

/** 删除当前选中的 KB（后端禁止删 default，按钮已前置禁用兜底）：确认后刷新，选中回落 default/第一个。 */
async function handleDeleteKb(): Promise<void> {
  if (!requireRendererLogin('请先登录后再删除知识库')) return;
  const kbId = view.kbId;
  if (!kbId || kbId === 'default') return;
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

/** 删除后刷新列表与概览；删的是当前选中页时清空详情栏。 */
async function refreshAfterDelete(deletedIds: string[]): Promise<void> {
  if (view.selectedId && deletedIds.includes(view.selectedId)) {
    view.selectedId = null;
  }
  await loadPages();
  await loadKbSummary();
}

async function handleDeletePage(pageId: string, title: string): Promise<void> {
  if (!requireRendererLogin('请先登录后再删除页面')) return;
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
  if (!requireRendererLogin('请先登录后再删除页面')) return;
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

async function handlePageEditorSubmit(form: HTMLFormElement): Promise<void> {
  if (!view.kbId || pageEditorSubmitting) return;
  const data = new FormData(form);
  const title = String(data.get('title') || '').trim();
  if (!title) {
    notify('请输入页面标题');
    return;
  }
  pageEditorSubmitting = true;
  renderShell();
  try {
    const content = String(data.get('content') || '');
    if (pageEditorMode === 'edit' && view.selectedId) {
      const existing = view.pageDetails[view.selectedId] ?? view.pages.find((p) => p.id === view.selectedId);
      const result = await backendApi.wikiUpdatePage(
        view.selectedId,
        {
          title,
          content,
          tags: existing?.tags ?? [],
          sources: existing?.sources ?? [],
          relations: existing?.relations ?? [],
        },
        view.kbId,
      );
      view.pageDetails = { ...view.pageDetails, [result.page.id]: result.page };
      view.sourceTitles = { ...view.sourceTitles, ...(result.source_titles || {}) };
      view.sourceFiles = { ...view.sourceFiles, ...(result.source_files || {}) };
      view.sourcePages = {
        ...view.sourcePages,
        [result.page.id]: result.source_pages ?? view.sourcePages[result.page.id] ?? [],
      };
      view.relationPages = {
        ...view.relationPages,
        [result.page.id]: result.relation_pages ?? view.relationPages[result.page.id] ?? [],
      };
    } else {
      const result = await backendApi.wikiCreatePage(
        { title, content, page_type: String(data.get('page_type') || 'topic') as WikiPageType, status: String(data.get('status') || 'published') as WikiPage['status'] },
        view.kbId,
      );
      view.selectedId = result.page.id;
      view.pageDetails = { ...view.pageDetails, [result.page.id]: result.page };
      view.sourceTitles = { ...view.sourceTitles, ...(result.source_titles || {}) };
      view.sourceFiles = { ...view.sourceFiles, ...(result.source_files || {}) };
    }
    pageEditorMode = null;
    notify('Wiki 页面已保存');
    await loadPages();
    invalidateWikiGraph();
  } catch (err) {
    notify(`保存页面失败：${errMsg(err)}`);
  } finally {
    pageEditorSubmitting = false;
    renderShell();
  }
}

// ── 图谱视图（Phase 3） ──

/** 图谱节点点击回调（wiki-graph 注入）：选中页面并在右栏显示详情，对齐 web onSelectPage。 */
function handleGraphSelectPage(pageId: string): void {
  if (pageId === view.selectedId) return;
  view.selectedId = pageId;
  renderShell();
  // 图谱是全量数据，节点可能不在已分页加载的列表里：只要本地没有完整详情就拉取。
  if (!view.pageDetails[pageId]) {
    const known = view.pages.find((p) => p.id === pageId);
    if (!known || !known.content) void loadPageDetail(pageId);
  }
}

/** Wiki Agent 引用页面时，在当前 Wiki 中栏打开，而不是跳回主聊天或弹 overlay。 */
export function openWikiPageInHub(pageId: string): boolean {
  const root = $('#wiki-page-root');
  if (!root || state.activeTab !== 'wiki' || !pageId) return false;
  handleGraphSelectPage(pageId);
  return true;
}

/**
 * 选中某个页面并渲染：清掉 vault 文档选中态、可选展开文件树祖先路径，
 * 缺完整正文时异步拉取。供双链 / 来源 pill / 关系卡片 / 图谱点击共用。
 */
function selectWikiPage(pageId: string, opts: { expandTree?: boolean } = {}): void {
  if (pageId === view.selectedId && !view.selectedDocumentName) return;
  view.selectedDocumentName = null;
  view.vaultDocument = null;
  view.selectedId = pageId;
  const page = view.pages.find((p) => p.id === pageId);
  if (opts.expandTree && page && view.view === 'tree') {
    for (const p of ancestorPaths(page.file_path)) view.expandedPaths.add(p);
  }
  renderShell();
  if (page && !view.pageDetails[pageId] && !page.content) {
    void loadPageDetail(pageId);
  }
}

/**
 * 按标题打开 Wiki 页面：先在本地 pages 列表里找，找不到再走后端搜索。
 * 用于双链 [[标题]] 与来源 pill 点击导航。
 */
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

// ── 事件绑定 ──

function bindEvents(): void {
  // 选择器必须限定在本页容器内：各 tab 页常驻 DOM（隐藏而非移除），
  // document 级 $$ 会误绑到其他页面的同名 data-* 元素（如 skills 页的 [data-refresh]）。
  const root = $('#wiki-page-root');
  if (!root) return;
  const $$w = <T extends Element = HTMLElement>(selector: string): T[] =>
    Array.from(root.querySelectorAll(selector)) as T[];

  const kbSelect = $('#wiki-kb-select') as HTMLSelectElement | null;
  kbSelect?.addEventListener('change', () => {
    const id = kbSelect.value;
    if (!id || id === view.kbId) return;
    view.kbId = id;
    void reloadAll();
  });

  $$w('[data-wiki-view]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const next = (btn.getAttribute('data-wiki-view') as WikiListView) || 'timeline';
      if (next === view.view) return;
      view.view = next;
      renderShell();
    });
  });

  $$w('[data-refresh]').forEach((btn) => {
    btn.addEventListener('click', () => {
      if (!requireRendererLogin('请先登录后再刷新知识库')) return;
      btn.classList.add('is-spinning');
      void reloadAll().finally(() => {
        window.setTimeout(() => btn.classList.remove('is-spinning'), 500);
      });
    });
  });
  $$w('[data-wiki-tour]').forEach((btn) => {
    btn.addEventListener('click', startWikiTour);
  });

  $$w('[data-upload]').forEach((btn) => {
    btn.addEventListener('click', () => {
      if (!requireRendererLogin('请先登录后再上传文件')) return;
      fireWikiAgentEntry(undefined, true);
    });
  });

  // ── 新建 KB 内联表单 ──
  $$w('[data-kb-delete]').forEach((btn) => {
    btn.addEventListener('click', () => void handleDeleteKb());
  });

  // ── 批量选择模式 ──
  $$w('[data-batch-toggle]').forEach((btn) => {
    btn.addEventListener('click', () => {
      view.selecting = true;
      view.selectedIds = new Set<string>();
      renderShell();
    });
  });
  $$w('[data-batch-done]').forEach((btn) => {
    btn.addEventListener('click', () => {
      view.selecting = false;
      view.selectedIds = new Set<string>();
      renderShell();
    });
  });
  $$w('[data-select-all]').forEach((btn) => {
    btn.addEventListener('click', () => {
      view.selectedIds = new Set(view.pages.map((p) => p.id));
      renderShell();
    });
  });
  $$w('[data-deselect-all]').forEach((btn) => {
    btn.addEventListener('click', () => {
      view.selectedIds = new Set<string>();
      renderShell();
    });
  });
  $$w('[data-bulk-delete]').forEach((btn) => {
    btn.addEventListener('click', () => void handleBulkDelete());
  });

  $$w('[data-delete-id]').forEach((btn) => {
    btn.addEventListener('click', (e) => {
      // 删除按钮在条目内部，阻止冒泡避免触发选中/打开详情。
      e.stopPropagation();
      const id = btn.getAttribute('data-delete-id') ?? '';
      if (!id) return;
      void handleDeletePage(id, btn.getAttribute('data-delete-title') ?? '');
    });
  });
  $$w('[data-kb-create-toggle]').forEach((btn) => {
    btn.addEventListener('click', () => {
      kbCreateOpen = !kbCreateOpen;
      renderShell();
      if (kbCreateOpen) {
        root.querySelector<HTMLInputElement>('[data-kb-create-input]')?.focus();
      }
    });
  });
  $$w('[data-kb-create-cancel]').forEach((btn) => {
    btn.addEventListener('click', () => {
      kbCreateOpen = false;
      kbCreateDraft = '';
      renderShell();
    });
  });
  $$w('[data-kb-create-submit]').forEach((btn) => {
    btn.addEventListener('click', () => void handleCreateKbSubmit());
  });
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

  $$w('[data-page-create]').forEach((btn) => {
    btn.addEventListener('click', () => {
      pageEditorMode = 'create';
      renderShell();
      root.querySelector<HTMLInputElement>('[data-page-editor] input[name="title"]')?.focus();
    });
  });
  $$w('[data-page-edit]').forEach((btn) => {
    btn.addEventListener('click', () => {
      pageEditorMode = 'edit';
      renderShell();
    });
  });
  $$w('[data-page-editor-cancel]').forEach((btn) => {
    btn.addEventListener('click', () => {
      pageEditorMode = null;
      renderShell();
    });
  });
  $$w<HTMLFormElement>('[data-page-editor]').forEach((form) => {
    form.addEventListener('submit', (event) => {
      event.preventDefault();
      void handlePageEditorSubmit(form);
    });
  });

  // ── 列表栏分栏拖拽：拖动中只改 DOM 样式（不重渲染不丢事件），mouseup 才持久化 ──
  $$w('[data-wiki-sash]').forEach((sash) => {
    sash.addEventListener('mousedown', (e) => {
      const me = e as MouseEvent;
      const pane = root.querySelector<HTMLElement>('.wiki-list-pane');
      if (!pane) return;
      const startX = me.clientX;
      const startWidth = listWidth;
      sash.classList.add('is-dragging');
      document.body.classList.add('wiki-resizing');
      const onMove = (ev: MouseEvent): void => {
        listWidth = clampListWidth(startWidth + (ev.clientX - startX));
        setRuntimeStyle(pane, 'width', `${listWidth}px`);
      };
      const onUp = (): void => {
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
        sash.classList.remove('is-dragging');
        document.body.classList.remove('wiki-resizing');
        persistListWidth();
      };
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
      me.preventDefault();
    });
    // 双击复位默认宽度
    sash.addEventListener('dblclick', () => {
      listWidth = WIKI_LIST_DEFAULT_WIDTH;
      persistListWidth();
      renderShell();
    });
  });


  $$w('[data-load-more]').forEach((btn) => {
    btn.addEventListener('click', () => void loadMorePages());
  });

  $$w('[data-page-id]').forEach((item) => {
    item.addEventListener('click', () => {
      const id = item.getAttribute('data-page-id');
      if (!id) return;
      view.selectedDocumentName = null;
      view.vaultDocument = null;
      // 批量选择模式：点击条目切换选中而非打开详情。
      if (view.selecting) {
        if (view.selectedIds.has(id)) view.selectedIds.delete(id);
        else view.selectedIds.add(id);
        renderShell();
        return;
      }
      if (id === view.selectedId) return;
      view.selectedDocumentName = null;
      view.vaultDocument = null;
      view.selectedId = id;
      const page = view.pages.find((p) => p.id === id);
      if (page && view.view === 'tree') {
        for (const p of ancestorPaths(page.file_path)) view.expandedPaths.add(p);
      }
      renderShell();
      if (page && !view.pageDetails[id] && !page.content) {
        void loadPageDetail(id);
      }
    });
  });

  $$w('[data-tree-path]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const path = btn.getAttribute('data-tree-path') ?? '';
      if (view.expandedPaths.has(path)) view.expandedPaths.delete(path);
      else view.expandedPaths.add(path);
      renderShell();
    });
  });

  $$w('[data-vault-document]').forEach((item) => {
    item.addEventListener('click', () => {
      const name = item.getAttribute('data-vault-document');
      if (name === 'Home.md' || name === 'index.md') void loadVaultDocument(name);
    });
  });

  $$w('[data-rel-title]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const title = btn.getAttribute('data-rel-title') ?? '';
      void resolveAndOpenWikiPage(title);
    });
  });

  // 双链关系卡片点击：跳转到关联页面（结构化关系，比 data-rel-title 更精确）
  $$w('[data-related-page-id]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const pageId = btn.getAttribute('data-related-page-id') ?? '';
      if (pageId) selectWikiPage(pageId, { expandTree: true });
    });
  });

  // 来源 pill 点击：跳转到来源摘要页
  $$w('[data-source-page-id]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const pageId = btn.getAttribute('data-source-page-id') ?? '';
      if (pageId) selectWikiPage(pageId, { expandTree: true });
    });
  });

  // Home.md 推荐问题点击：把问题直接发给 Wiki Agent
  $$w('[data-wiki-ask]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const question = btn.getAttribute('data-wiki-ask') ?? '';
      fireWikiAgentPrompt(question);
    });
  });
}

// ── 对外入口 ──

export function renderWikiPage(): void {
  renderShell();
}

/** 切入 Wiki tab 时调用：首次加载 / 显式刷新。 */
export async function refreshWikiData(): Promise<void> {
  // 未登录只渲染引导态，不发请求（避免 401 toast 与误导空态）。
  if (!isRendererLoggedIn()) {
    renderShell();
    return;
  }
  if (view.loaded) {
    renderShell();
    return;
  }
  await reloadAll();
  // 仅首次加载成功才置位：失败（如网络异常）时保持 false，下次切回 tab 自动重试。
  if (!view.kbsLoadFailed) {
    view.loaded = true;
    maybeStartWikiTourOnce();
  }
}

export function activateWikiPage(): void {
  void refreshWikiData();
}

/** Legacy tab hook kept for feature/test callers while the shell owns navigation. */
export function bindWikiTab(onTab: () => void): () => void {
  const tab = document.querySelector('[data-tab="wiki"]');
  const handleClick = (): void => {
    onTab();
    void refreshWikiData();
  };
  tab?.addEventListener('click', handleClick);
  return () => tab?.removeEventListener('click', handleClick);
}

export function bindWikiPageLifecycle(): () => void {
  // 登录态变化（登录成功 / 退出）后重置缓存，下次进入 tab 重新拉取。
  const onLoginChanged = (): void => {
    view = initialViewState();
    initializedKbIds.clear();
    autoInitAttempted = false;
    kbCreateOpen = false;
    kbCreateDraft = '';
    kbCreateSubmitting = false;
    pageEditorMode = null;
    pageEditorSubmitting = false;
    invalidateWikiGraph();
    renderShell();
    // 登录成功且当前停在 Wiki tab：立即自动拉取（登出则停在登录引导态）。
    if (isRendererLoggedIn() && state.activeTab === 'wiki') {
      void refreshWikiData();
    }
  };
  const onWikiChanged = (event: Event): void => {
    const changes = (
      (event as CustomEvent<{ changes?: Array<{ kb_id?: string }> }>).detail?.changes ?? []
    );
    if (!view.kbId || !changes.some((change) => change.kb_id === view.kbId)) return;
    invalidateWikiGraph();
    void reloadAll();
  };
  window.addEventListener('user:login-changed', onLoginChanged);
  window.addEventListener('wiki:changed', onWikiChanged);
  return () => {
    window.removeEventListener('user:login-changed', onLoginChanged);
    window.removeEventListener('wiki:changed', onWikiChanged);
    detailFoldHandle?.dispose();
    detailFoldHandle = null;
    detailRenderKey = '';
    wikiWorkspaceView = null;
  };
}

export async function initWikiPage(): Promise<void> {
  renderShell();
}
