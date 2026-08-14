/**
 * Wiki 知识库页：左侧 Wiki Agent 对话主区 + 右侧知识库面板（目录+详情）（对齐 web WikiHub）。
 *
 * 数据源：GET /api/wiki/kbs + /api/wiki/pages（brief=1 分页）+ /api/wiki/pages/{id} + /api/wiki/summary
 *         + /api/wiki/graph（Phase 3 图谱，由 features/wiki-graph.ts 消费）
 * 写操作：POST /api/wiki/upload（主进程 gateway:upload IPC）+ /api/wiki/ingest(+cancel)
 *         + DELETE /api/wiki/pages/{id} + DELETE /api/wiki/pages（批量）
 *         + POST /api/wiki/kbs（新建 KB，内联表单）+ POST /api/wiki/init（无 KB 自动初始化）
 *         + DELETE /api/wiki/kbs/{id}（删除 KB，default 不可删）
 *
 * 布局（左对话主区 / 右知识库面板）：
 *   1. 页头：KB 选择器（下拉）+ 新建 KB + 上传 + 批量管理 + 知识库面板收起/展开
 *      （「问 Wiki」已下线：对话面板常驻主区，无需入口按钮）
 *   2. 上传任务面板：每个 source 的进度条 + 阶段文案 + 错误态；进度经 WS
 *      wiki_ingest_progress 帧（chat-controller 回调转发）实时更新
 *   3. 左侧主区：Wiki Agent 对话面板（features/wiki-agent.ts 挂载，常驻，弹性宽度）
 *   4. 右侧知识库面板（.wiki-browser-pane，可拖拽调宽、可整体收起）：
 *      目录列表：分页「加载更多」；条目 = 标题 + 类型徽标 + 更新时间 + 摘要；
 *      单条 ⋯ 操作菜单（打开/重命名/删除）；批量管理模式下条目变 checkbox 选择；
 *      「图谱」视图下列表区域替换为图谱画布（features/wiki-graph.ts，本文件只注入容器 + 回调）
 *   5. 分栏把手：对话主区与知识库面板之间可拖拽调宽（localStorage 持久化，双击复位）
 *   6. 面板内详情：标题 + 元信息 + Markdown 正文；未选中时显示 KB 概览 / 空态；
 *      Home.md（知识库概览）的「推荐问题」小节会被后处理成提问按钮，
 *      点击直接把问题发给 Wiki Agent（decorateHomeQuestions + [data-wiki-ask] 委托）
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
import { showContextMenu, type ContextMenuItem } from '../lib/context-menu';
import { showConfirmDialog, showPromptDialog } from '../ui-feedback';
import { __resetWikiGraphForTest, invalidateWikiGraph, mountWikiGraph } from './wiki-graph';
import { mountWikiEditor, type WikiEditorHandle } from './wiki-editor';
import { maybeStartWikiTourOnce, startWikiTour } from './wiki-tour';

// ── Wiki Agent 入口（Phase 4） ──
// 「上传」按钮（打开对话区附件选择）与失败任务「让 AI 处理」共用同一挂点；回调由 index.ts
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
  /** 聚焦对话主区并打开标准 Composer 附件选择。 */
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

/** Home.md 推荐问题点击：把问题直接发给 Wiki Agent。 */
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
  const parts = normalizeVaultPath(filePath).split('/').filter(Boolean);
  const paths: string[] = [];
  for (let i = 1; i < parts.length; i++) {
    paths.push(parts.slice(0, i).join('/'));
  }
  return paths;
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

// ── 详情面板多 Tab ──

/** 详情面板顶部打开的 tab：Wiki 页面或 vault 文档（Home.md/index.md）。 */
export type WikiOpenTab = { kind: 'page'; id: string } | { kind: 'doc'; name: 'Home.md' | 'index.md' };

/** tab 的稳定标识（DOM data-* 与详情签名比对共用）。 */
function tabKey(tab: WikiOpenTab): string {
  return tab.kind === 'page' ? `page:${tab.id}` : `doc:${tab.name}`;
}

/** 追加 tab（同 key 去重，已存在时返回原数组）。 */
function openTabUnique(tabs: WikiOpenTab[], tab: WikiOpenTab): WikiOpenTab[] {
  return tabs.some((t) => tabKey(t) === tabKey(tab)) ? tabs : [...tabs, tab];
}

/** 关闭 tab：返回新列表与下一个激活 key；关闭激活 tab 时相邻优先右侧，无相邻则为 null。 */
function closeTabByKey(
  tabs: WikiOpenTab[],
  key: string,
  activeKey: string | null,
): { tabs: WikiOpenTab[]; nextActiveKey: string | null } {
  const index = tabs.findIndex((t) => tabKey(t) === key);
  if (index < 0) return { tabs, nextActiveKey: activeKey };
  const next = tabs.filter((_, i) => i !== index);
  if (activeKey !== key) return { tabs: next, nextActiveKey: activeKey };
  const neighbor = next[index] ?? next[index - 1] ?? null;
  return { tabs: next, nextActiveKey: neighbor ? tabKey(neighbor) : null };
}

// ── 详情编辑器组（VSCode 式双组拆分：最多 2 组，row/column 两方向，同一页面全局只开一个组） ──

/** 一个详情编辑器组：独立 tab 栏 + 内容区 + 编辑器实例 + 保存状态。 */
export interface WikiDetailGroup {
  id: string;
  tabs: WikiOpenTab[];
  selectedId: string | null;
  selectedDocumentName: 'Home.md' | 'index.md' | null;
}

/** 组 id 自增（g1/g2…）；reloadAll/reset 重建状态后新组取新 id，旧保活签名随之失效。 */
let groupSeq = 0;

function createDetailGroup(): WikiDetailGroup {
  groupSeq += 1;
  return { id: `g${groupSeq}`, tabs: [], selectedId: null, selectedDocumentName: null };
}

/** 当前聚焦组（树点击 / 外部打开落在该组）；activeGroupId 失效时回落第一组。 */
function activeGroup(): WikiDetailGroup {
  return view.detailGroups.find((g) => g.id === view.activeGroupId) ?? view.detailGroups[0];
}

function groupById(groupId: string): WikiDetailGroup | null {
  return view.detailGroups.find((g) => g.id === groupId) ?? null;
}

/** 组内激活 tab 的 key（组内未打开任何详情时为 null）。 */
function groupActiveKey(group: WikiDetailGroup): string | null {
  if (group.selectedId) return `page:${group.selectedId}`;
  if (group.selectedDocumentName) return `doc:${group.selectedDocumentName}`;
  return null;
}

/** 跨组查重：tab key 所在的组（拆分/移动/打开共用以保证全局唯一）。 */
function findTabOwner(key: string): WikiDetailGroup | null {
  return view.detailGroups.find((g) => g.tabs.some((t) => tabKey(t) === key)) ?? null;
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
  /** 详情编辑器组（始终 ≥1 个，初始 1 个空组；最多 2 个）。 */
  detailGroups: WikiDetailGroup[];
  /** 聚焦组 id：树点击 / 外部打开落在该组；点击组内任意处更新。 */
  activeGroupId: string;
  /** 两组排列方向（拆分动作时写入；单组时无意义）。 */
  groupOrientation: 'row' | 'column';
  /** vault 文档（Home.md/index.md）内容缓存（按文档名；两组可同时各开一份文档）。 */
  vaultDocuments: Partial<Record<'Home.md' | 'index.md', WikiVaultDocument>>;
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
  const detailGroup = createDetailGroup();
  return {
    kbs: [],
    kbId: null,
    pages: [],
    pageOffset: 0,
    hasMore: false,
    loading: false,
    loadingMore: false,
    view: 'timeline',
    detailGroups: [detailGroup],
    activeGroupId: detailGroup.id,
    groupOrientation: 'row',
    vaultDocuments: {},
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

// ── 分栏宽度：拖拽 + 双击复位 + localStorage 持久化（知识库面板 / 图谱视图共用一套机制） ──

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
 * axis = 'y' 用于上下堆叠的组间把手（clientY 驱动，cursor 换 row-resize）。
 */
export function bindPaneSash(
  sash: HTMLElement,
  opts: {
    sign?: 1 | -1;
    axis?: 'x' | 'y';
    startWidth: () => number;
    onStart?: () => void;
    onDrag: (w: number) => void;
    onCommit: (w: number) => void;
    onReset: () => void;
  },
): void {
  const sign = opts.sign ?? 1;
  const axis = opts.axis ?? 'x';
  const resizingClass = axis === 'y' ? 'wiki-resizing-y' : 'wiki-resizing';
  const coord = (e: MouseEvent): number => (axis === 'y' ? e.clientY : e.clientX);
  sash.addEventListener('mousedown', (e) => {
    const startPos = coord(e);
    const startW = opts.startWidth();
    let current = startW;
    sash.classList.add('is-dragging');
    document.body.classList.add(resizingClass);
    opts.onStart?.();
    const onMove = (ev: MouseEvent): void => {
      current = startW + sign * (coord(ev) - startPos);
      opts.onDrag(current);
    };
    const onUp = (): void => {
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
      sash.classList.remove('is-dragging');
      document.body.classList.remove(resizingClass);
      opts.onCommit(current);
    };
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
    e.preventDefault();
  });
  sash.addEventListener('dblclick', () => opts.onReset());
}

const WIKI_BROWSER_DEFAULT_WIDTH = 500;
/** 右侧知识库面板（目录+详情）宽度：可拖拽 + 持久化。 */
const browserWidthStore = createPaneWidthStore({ key: 'crew.desktop.wikiBrowserWidth.v2', min: 340, max: 1000, vwFactor: 0.7 });
const WIKI_CATALOG_DEFAULT_WIDTH = 240;
/** 面板内目录宽度：可拖拽 + 持久化（仅列表/树/类型视图；图谱模式目录走 1.5:1 比例分配，不内联宽度）。 */
const catalogWidthStore = createPaneWidthStore({ key: 'crew.desktop.wikiCatalogWidth.v1', min: 200, max: 480, vwFactor: 0.5 });
/** 图谱视图下面板宽度（仅图谱视图）：null = 沿用 browserWidth；拖拽后固定为像素值并持久化，双击复位。 */
const graphWidthStore = createPaneWidthStore({ key: 'crew.desktop.wikiGraphWidth.v2', min: 280, vwFactor: 0.7 });
/** 图谱画布（目录区）宽度（仅图谱视图）：null = 与详情 1.5:1 弹性分配；拖拽后固定像素并持久化，双击恢复弹性。 */
const graphCanvasWidthStore = createPaneWidthStore({ key: 'crew.desktop.wikiGraphCanvasWidth.v1', min: 240, max: 800, vwFactor: 0.5 });

/** 详情双组比例（百分比，第一组占比）：钳制 20~80，localStorage 持久化，双击复位 50（清存储）。 */
const WIKI_DETAIL_SPLIT_KEY = 'crew.desktop.wikiDetailSplit.v1';
const groupSplitStore = {
  clamp(ratio: number): number {
    if (!Number.isFinite(ratio)) return 50;
    return Math.max(20, Math.min(80, Math.round(ratio)));
  },
  load(): number | null {
    try {
      const raw = localStorage.getItem(WIKI_DETAIL_SPLIT_KEY);
      if (!raw) return null;
      const parsed = parseInt(raw, 10);
      return Number.isFinite(parsed) ? groupSplitStore.clamp(parsed) : null;
    } catch {
      return null;
    }
  },
  persist(ratio: number | null): void {
    try {
      if (ratio == null) localStorage.removeItem(WIKI_DETAIL_SPLIT_KEY);
      else localStorage.setItem(WIKI_DETAIL_SPLIT_KEY, String(ratio));
    } catch {
      /* quota / disabled */
    }
  },
};

let browserWidth = browserWidthStore.load() ?? WIKI_BROWSER_DEFAULT_WIDTH;
let catalogWidth = catalogWidthStore.load() ?? WIKI_CATALOG_DEFAULT_WIDTH;
let graphWidth: number | null = graphWidthStore.load();
let graphCanvasWidth: number | null = graphCanvasWidthStore.load();
/** 详情双组第一组占比（%）：两组时生效，组间把手拖拽更新。 */
let groupSplitRatio = groupSplitStore.load() ?? 50;

const WIKI_BROWSER_OPEN_KEY = 'crew.desktop.wikiBrowserOpen.v1';
/** 右侧知识库面板展开/收起（持久化，默认展开）。收起走 CSS 隐藏，DOM 保留，编辑器/详情保活不受影响。 */
let wikiBrowserOpen = ((): boolean => {
  try {
    return localStorage.getItem(WIKI_BROWSER_OPEN_KEY) !== '0';
  } catch {
    return true;
  }
})();

/** 收起/展开右侧知识库面板并持久化（页头按钮与 Wiki Agent 面板头按钮共用）。 */
export function toggleWikiBrowser(): void {
  wikiBrowserOpen = !wikiBrowserOpen;
  try {
    localStorage.setItem(WIKI_BROWSER_OPEN_KEY, wikiBrowserOpen ? '1' : '0');
  } catch {
    /* quota / disabled */
  }
  renderShell();
}
/** 正在加载详情的 pageId（防重复点击重复请求）。 */
const loadingDetails = new Set<string>();
/** 正在加载的 vault 文档名（按文档名；两组可同时各开一份文档）。 */
const loadingVaultDocs = new Set<string>();

/** 测试钩子：覆盖 view 状态（单测用）。 */
export function __setWikiViewForTest(patch: Partial<WikiViewState>): void {
  view = { ...view, ...patch };
}

/** 视图状态整体复位（登录态变化 / 测试钩子共用，防两处漂移漏清）。 */
function resetWikiViewState(): void {
  view = initialViewState();
  graphWidth = null;
  graphCanvasWidth = null;
  // 收起状态重新从 localStorage 读取（登录态变化后尊重用户持久化偏好，测试 stub 恒 null → 默认展开）
  try {
    wikiBrowserOpen = localStorage.getItem(WIKI_BROWSER_OPEN_KEY) !== '0';
  } catch {
    wikiBrowserOpen = true;
  }
  loadSeq = 0;
  autoInitAttempted = false;
  initializedKbIds.clear();
  kbCreateOpen = false;
  kbCreateDraft = '';
  kbCreateSubmitting = false;
  loadingDetails.clear();
  loadingVaultDocs.clear();
  // 编辑器组运行时装（编辑器/折叠/计时器/脏标记）全部释放，防 observer 与计时器泄漏。
  for (const groupId of Array.from(groupDetails.keys())) disposeGroupDetail(groupId);
  lastDetailSigs.clear();
  lastGroupsKey = null;
  tabDrag = null;
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

/** 工具栏/标签用线性图标（crew-ui-symbols 雪碧图，风格与全局一致）。 */
function uiIcon(symbolId: string): string {
  return `<svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true"><use href="./crew-ui-symbols.svg#${symbolId}"></use></svg>`;
}

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

/** 单条 ⋯ 操作菜单按钮（批量选择模式下隐藏），与会话条目的 ⋯ 菜单同款。 */
function pageMenuBtnHtml(page: WikiPage): string {
  if (view.selecting) return '';
  return `<button type="button" class="wiki-item__menu" data-page-menu="${escapeHtml(page.id)}" title="页面操作" aria-label="页面操作">${uiIcon('icon-more')}</button>`;
}

function listItemHtml(page: WikiPage, compact = false): string {
  const active = page.id === activeGroup().selectedId ? ' is-active' : '';
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
      ${pageMenuBtnHtml(page)}
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
        const active = node.name === activeGroup().selectedDocumentName ? ' is-active' : '';
        return `
          <li class="wiki-tree__item wiki-tree__item--document${active}" data-vault-document="${escapeHtml(node.name)}">
            <button type="button" class="wiki-tree__label" style="padding-left: ${depth * 16 + 6}px">
              <span class="wiki-badge">文件</span>
              <span class="wiki-tree__title">${escapeHtml(vaultDocumentLabel(node.name))}</span>
            </button>
          </li>`;
      }
      const active = node.page.id === activeGroup().selectedId ? ' is-active' : '';
      const { checkedClass, checkHtml } = selectionMark(node.page.id);
      // ⋯ 菜单按钮是 <li> 的独立子元素（button 不能嵌套 button），
      // 由 CSS 绝对定位到文件名左侧的缩进空隙里，悬停时淡入。
      return `
        <li class="wiki-tree__item${view.selecting ? ' wiki-item--selecting' : ''}${active}${checkedClass}" data-page-id="${escapeHtml(node.page.id)}" style="--tree-indent: ${treeIndentPx(depth)}px">
          <button type="button" class="wiki-tree__label" style="padding-left: var(--tree-indent)">
            ${checkHtml}
            ${typeBadge(node.page.page_type)}
            <span class="wiki-tree__title">${escapeHtml(node.page.title)}</span>
          </button>
          ${pageMenuBtnHtml(node.page)}
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

/** 页面详情 article HTML（Wiki 页详情区与 Phase 4 对话流 overlay 共用）。所有插值 escapeHtml。 */
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

/** 组内顶部的多 tab 栏（pill chip，视觉对齐 Inspector 的 workspace tab；空组不渲染）。chip 可拖拽（拆分/移动）。 */
function wikiGroupTabsHtml(group: WikiDetailGroup): string {
  if (group.tabs.length === 0) return '';
  const active = groupActiveKey(group);
  const chips = group.tabs
    .map((tab) => {
      const key = tabKey(tab);
      const title =
        tab.kind === 'doc'
          ? vaultDocumentLabel(tab.name)
          : view.pageDetails[tab.id]?.title ?? view.pages.find((p) => p.id === tab.id)?.title ?? tab.id;
      return `<div class="wiki-tab${key === active ? ' is-active' : ''}" data-wiki-tab-chip="${escapeHtml(key)}" draggable="true">
        <button type="button" class="wiki-tab__select" data-wiki-tab="${escapeHtml(key)}" title="${escapeHtml(title)}"><span class="wiki-tab__label">${escapeHtml(title)}</span></button>
        <button type="button" class="wiki-tab__close" data-wiki-tab-close="${escapeHtml(key)}" title="关闭" aria-label="关闭 ${escapeHtml(title)}">×</button>
      </div>`;
    })
    .join('');
  return `<div class="wiki-tabs" role="tablist">${chips}</div>`;
}

function detailHtml(group: WikiDetailGroup): string {
  if (group.selectedDocumentName) {
    const doc = view.vaultDocuments[group.selectedDocumentName];
    if (loadingVaultDocs.has(group.selectedDocumentName) || !doc) {
      return `<div class="wiki-detail__empty"><p class="wiki-detail__empty-hint">加载文档中…</p></div>`;
    }
    const isHome = doc.name === 'Home.md';
    return `
      <article class="wiki-detail${isHome ? ' wiki-home-document' : ''}">
        <header class="wiki-detail__header">
          <div class="wiki-detail__badges"><span class="wiki-badge">${isHome ? '概览' : '文件'}</span></div>
          <h2 class="wiki-detail__title">${escapeHtml(vaultDocumentLabel(doc.name))}</h2>
          <div class="wiki-detail__meta">
            <span>更新于 ${escapeHtml(formatWikiTime(doc.updated_at))}</span>
          </div>
        </header>
        <div class="md-body chat-markdown wiki-detail__content" data-wiki-fold-content></div>
      </article>`;
  }
  if (!group.selectedId) {
    // 两组时的空组：纯空态提示（对齐 VSCode 空编辑器组），不再重复展示 KB 概览。
    if (view.detailGroups.length > 1) {
      return `
        <div class="wiki-detail__empty">
          <p class="wiki-detail__empty-hint">拖拽 tab 到此处，或从目录打开页面</p>
        </div>`;
    }
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
        <p class="wiki-detail__empty-hint">选择右侧页面查看详情，或在左侧对话栏基于知识库提问</p>
      </div>`;
  }
  const selectedId = group.selectedId;
  if (loadingDetails.has(selectedId) && !view.pageDetails[selectedId]) {
    return `<div class="wiki-detail__empty"><p class="wiki-detail__empty-hint">加载页面详情中…</p></div>`;
  }
  const page = view.pageDetails[selectedId] ?? view.pages.find((p) => p.id === selectedId);
  if (!page) {
    return `<div class="wiki-detail__empty"><p class="wiki-detail__empty-hint">选择右侧页面查看详情</p></div>`;
  }
  return wikiDetailArticleHtml(page, {
    sourcePages: view.sourcePages[page.id],
    relationPages: view.relationPages[page.id],
  });
}

/** 单个编辑器组的运行时装：fold 增量渲染句柄（子树重建时先 dispose，防 observer 泄漏）、
 * TipTap 编辑器、自动保存计时器与脏标记（dirty 仅 scheduleWikiPageSave 置位；看一眼不置位，避免浏览也刷 updated_at）。 */
interface WikiGroupDetail {
  editor: WikiEditorHandle | null;
  fold: FoldedMarkdownHandle | null;
  timer: ReturnType<typeof setTimeout> | null;
  dirty: boolean;
}

/** 各编辑器组的运行时装（groupId → 实例）；组回收/重建时 dispose 并删除。 */
const groupDetails = new Map<string, WikiGroupDetail>();

function groupDetail(groupId: string): WikiGroupDetail {
  let detail = groupDetails.get(groupId);
  if (!detail) {
    detail = { editor: null, fold: null, timer: null, dirty: false };
    groupDetails.set(groupId, detail);
  }
  return detail;
}

let localSaveInFlight = false;
let ignoreWikiChangedUntil = 0;

/** 组容器（DOM 查询一律收敛到组内，避免双组时互相串扰）。 */
function groupContainerEl(groupId: string): HTMLElement | null {
  return document.querySelector<HTMLElement>(`#wiki-page-root [data-wiki-group="${groupId}"]`);
}

function setWikiSaveState(groupId: string, stateValue: 'dirty' | 'saving' | 'saved' | 'error'): void {
  const target = groupContainerEl(groupId)?.querySelector<HTMLElement>('[data-wiki-save-state]');
  if (!target) return;
  target.dataset.state = stateValue;
  target.textContent = {
    dirty: '等待保存…',
    saving: '保存中…',
    saved: '已保存',
    error: '保存失败，将在下次修改时重试',
  }[stateValue];
}

function pageDraftFromDom(groupId: string, page: WikiPage, content: string): WikiPage {
  const container = groupContainerEl(groupId);
  const value = (selector: string): string =>
    container?.querySelector<HTMLInputElement>(selector)?.value ?? '';
  return {
    ...page,
    title: value('[data-wiki-title]').trim() || page.title,
    content,
  };
}

async function saveWikiPageDraft(groupId: string, pageId: string): Promise<void> {
  if (!view.kbId) return;
  const group = groupById(groupId);
  const current = view.pageDetails[pageId];
  if (!current || !group || group.selectedId !== pageId) return;
  const draft = pageDraftFromDom(groupId, current, groupDetails.get(groupId)?.editor?.flush() ?? current.content ?? '');
  setWikiSaveState(groupId, 'saving');
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
    const detail = groupDetails.get(groupId);
    if (detail) detail.dirty = false;
    setWikiSaveState(groupId, 'saved');
    invalidateWikiGraph();
  } catch (error) {
    setWikiSaveState(groupId, 'error');
    notify(`保存 Wiki 页面失败：${errMsg(error)}`);
  } finally {
    localSaveInFlight = false;
  }
}

function scheduleWikiPageSave(groupId: string, pageId: string): void {
  const detail = groupDetail(groupId);
  detail.dirty = true;
  setWikiSaveState(groupId, 'dirty');
  if (detail.timer) clearTimeout(detail.timer);
  detail.timer = setTimeout(() => {
    detail.timer = null;
    void saveWikiPageDraft(groupId, pageId);
  }, 700);
}

/** 释放组的编辑器/折叠/计时器并从 Map 删除（组回收、子树重建、状态重置共用）。 */
function disposeGroupDetail(groupId: string): void {
  const detail = groupDetails.get(groupId);
  if (!detail) return;
  if (detail.timer) clearTimeout(detail.timer);
  detail.editor?.destroy();
  detail.fold?.dispose();
  groupDetails.delete(groupId);
}

/** 组内当前选中页有未保存编辑时立即落盘（切 tab/移组/重命名前的统一 flush 口）。 */
function flushGroupDirty(group: WikiDetailGroup): void {
  const detail = groupDetails.get(group.id);
  if (!group.selectedId || !detail?.editor || !detail.dirty) return;
  // 保存即刻在途：先清计时器与脏标记，防同 tick 链式调用（移组 + 切相邻 tab）重复 flush。
  if (detail.timer) {
    clearTimeout(detail.timer);
    detail.timer = null;
  }
  detail.dirty = false;
  void saveWikiPageDraft(group.id, group.selectedId);
}

async function resolveAndOpenWikiPage(title: string): Promise<boolean> {
  const normalized = title.trim().toLocaleLowerCase();
  const local = view.pages.find((page) =>
    [page.title, ...page.aliases].some((value) => value.trim().toLocaleLowerCase() === normalized),
  );
  if (local) {
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
    selectWikiPage(target.id, { expandTree: true });
    return true;
  } catch (error) {
    notify(`打开 Wiki 页面失败：${errMsg(error)}`);
    return false;
  }
}

/**
 * 组详情内容签名：renderShell 全量重建时，签名未变的组保留现有子树
 * （fold 增量渲染的 observer、TipTap 编辑器与已解析正文一并存活，避免每次点击都重跑 markdown 解析）。
 */
interface DetailSig {
  selectedId: string | null;
  page: WikiPage | null;
  sourceTitles: WikiSourceTitles;
  kbSummary: { summary: string; page_count?: number | undefined; source_count?: number | undefined; generated_at?: number | undefined; status: string } | null;
  loading: boolean;
  /** 组内 tab 列表 + 激活 tab：tab 增删/切换时组子树需重建 tab 栏。 */
  tabsKey: string;
}

function currentDetailSig(group: WikiDetailGroup): DetailSig {
  const selectedId = group.selectedId;
  return {
    selectedId,
    page: selectedId ? view.pageDetails[selectedId] ?? view.pages.find((p) => p.id === selectedId) ?? null : null,
    sourceTitles: view.sourceTitles,
    kbSummary: selectedId ? null : view.kbSummary,
    loading: selectedId ? loadingDetails.has(selectedId) : false,
    tabsKey: `${group.tabs.map(tabKey).join('|')}#${groupActiveKey(group) ?? ''}`,
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
    a.loading === b.loading &&
    a.tabsKey === b.tabsKey
  );
}

/** 各组上一轮渲染的详情签名（groupId → DetailSig）；组结构（数量/方向/id）另由 lastGroupsKey 兜底。 */
const lastDetailSigs = new Map<string, DetailSig>();
let lastGroupsKey: string | null = null;

/** 组结构签名：组数量/id/方向变化时强制所有组子树重建（保活比对的前提）。 */
function currentGroupsKey(): string {
  return `${view.groupOrientation}#${view.detailGroups.map((g) => g.id).join('|')}`;
}

/** 列表滚动记忆：renderShell 全量重建后按 视图+KB 恢复 scrollTop，避免点击条目滚动条跳回顶部。 */
let listScrollMemory: { key: string; top: number } | null = null;

/** 知识库面板内联宽度：列表/树/类型视图固定持久化宽度；图谱视图拖拽后固定像素，未拖拽时沿用 browserWidth。 */
function browserPaneStyleAttr(): string {
  if (view.view === 'graph' && graphWidth != null) return ` style="width: ${graphWidth}px"`;
  return ` style="width: ${browserWidth}px"`;
}

/** 目录/图谱画布内联宽度：列表/树/类型视图固定目录宽度，用 flex 简写输出，压过 CSS 里 240px/窄窗口 220px 的 flex-basis 档位；
 * 图谱模式未拖拽时走 1.5:1 比例分配不内联，拖拽后内联固定像素压过 --graph 的 flex 1.5。 */
function catalogPaneStyleAttr(): string {
  if (view.view === 'graph') return graphCanvasWidth != null ? ` style="flex: 0 0 ${graphCanvasWidth}px"` : '';
  return ` style="flex: 0 0 ${catalogWidth}px"`;
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

  // 视图标签只显示图标（对齐 web WikiHub），原文字保留在 title/aria-label 供悬停提示与无障碍。
  const viewTabs: Array<{ key: WikiListView; label: string; icon: string }> = [
    { key: 'timeline', label: '时间线', icon: 'icon-history' },
    { key: 'tree', label: '文件树', icon: 'icon-folder-line' },
    { key: 'type', label: '类型', icon: 'icon-tag' },
    { key: 'graph', label: '图谱', icon: 'icon-graph' },
  ];
  const tabs = viewTabs
    .map(
      (t) =>
        `<button type="button" class="hub-segment__item${view.view === t.key ? ' is-active' : ''}" data-wiki-view="${t.key}" title="${t.label}" aria-label="${t.label}">${uiIcon(t.icon)}</button>`,
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
    // 图谱视图：目录列表区域整体替换为图谱画布（挂载点由 mountWikiGraph 接管）。
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
              <p class="wiki-list__empty-hint">点击右上角「上传」，或直接拖拽文件到左侧问答栏</p>
            </div>`
          : `${listViewHtml()}${loadMoreHtml()}`;
    // 详情区：1~2 个编辑器组 + 组间把手（两组时），方向由拆分动作决定。
    const groupSashHtml =
      view.detailGroups.length > 1
        ? `<div class="wiki-sash wiki-sash--group" data-wiki-group-sash role="separator" aria-orientation="${view.groupOrientation === 'column' ? 'horizontal' : 'vertical'}" title="拖拽调整分组比例，双击复位"></div>`
        : '';
    const detailGroupsHtml = view.detailGroups
      .map((group, index) => {
        const flexStyle =
          view.detailGroups.length > 1
            ? ` style="flex: ${index === 0 ? groupSplitRatio : 100 - groupSplitRatio} 1 0"`
            : '';
        return `
          <section class="wiki-detail-group${group.id === view.activeGroupId ? ' is-focused' : ''}" data-wiki-group="${group.id}"${flexStyle}>
            ${wikiGroupTabsHtml(group)}
            <div class="wiki-detail-group__content">${detailHtml(group)}</div>
          </section>`;
      })
      .join(groupSashHtml);
    body = `
      <div class="wiki-body">
        <aside class="wiki-agent-pane" data-wiki-agent-panel aria-label="Wiki Agent 对话"></aside>
        <div class="wiki-sash" data-wiki-browser-sash role="separator" aria-orientation="vertical" title="拖拽调整知识库面板宽度，双击复位"></div>
        <div class="wiki-browser-pane${graphMode ? ' wiki-browser-pane--graph' : ''}"${browserPaneStyleAttr()}>
          <div class="wiki-list-pane"${catalogPaneStyleAttr()}>
            ${batchBarHtml()}
            <nav class="hub-segment wiki-view-tabs" aria-label="列表视图">${tabs}</nav>
            <div class="wiki-list-scroll${graphMode ? ' wiki-list-scroll--graph' : ''}">${listBody}</div>
          </div>
          ${graphMode
            ? '<div class="wiki-sash wiki-sash--inner" data-wiki-graph-canvas-sash role="separator" aria-orientation="vertical" title="拖拽调整图谱宽度，双击恢复弹性比例"></div>'
            : '<div class="wiki-sash wiki-sash--inner" data-wiki-catalog-sash role="separator" aria-orientation="vertical" title="拖拽调整目录宽度，双击复位"></div>'}
          <div class="wiki-detail-pane">
            <div class="wiki-detail-groups" data-orientation="${view.groupOrientation}">${detailGroupsHtml}</div>
          </div>
        </div>
      </div>`;
  }

  const uploadDisabled = !view.kbId;
  const batchToggle =
    view.kbId && !view.selecting && view.pages.length > 0 && view.view !== 'graph'
      ? `<button type="button" class="hub-refresh-btn" data-batch-toggle title="批量选择页面以批量删除" aria-label="批量管理">${uiIcon('icon-checks')}</button>`
      : '';
  // 重建代价高的三棵子树在状态未变时保留活节点，避免每次点击都推倒重来：
  // 对话面板（整段会话重渲染 + markdown 重解析 + 强制滚底）、详情栏（fold observer 与
  // 已解析正文）、图谱画布（SVG 全量重建 + 逐节点重绑事件，wiki-graph 内部另有签名比对）。
  const liveAgentPanel = root.querySelector<HTMLElement>('[data-wiki-agent-panel]');
  const keepAgentPanel = view.kbId && liveAgentPanel?.dataset.kbId === view.kbId ? liveAgentPanel : null;
  // 面板节点虽被保留，但 innerHTML 重建会把它短暂 detach，浏览器把内部滚动位置
  // 重置为 0（对话跳回最早消息）。与 listScrollMemory 同理：先记后恢复。
  const agentMessagesScrollTop =
    keepAgentPanel?.querySelector<HTMLElement>('[data-wiki-agent-messages]')?.scrollTop ?? 0;
  // 组结构未变时按组比对签名收集可保留的子树；结构（数量/id/方向）一变全部重建。
  const groupsKeyNow = currentGroupsKey();
  const detailSigNowByGroup = new Map(view.detailGroups.map((g) => [g.id, currentDetailSig(g)] as const));
  const keepGroups = new Map<string, HTMLElement>();
  if (lastGroupsKey === groupsKeyNow) {
    for (const group of view.detailGroups) {
      const live = root.querySelector<HTMLElement>(`[data-wiki-group="${group.id}"]`);
      const lastSig = lastDetailSigs.get(group.id);
      const sigNow = detailSigNowByGroup.get(group.id);
      // vault 文档（Home.md/index.md）走 fold 增量渲染，不在签名内，选中即强制重建（对齐原单面板行为）。
      if (live && !group.selectedDocumentName && lastSig && sigNow && sameDetailSig(lastSig, sigNow)) {
        keepGroups.set(group.id, live);
      }
    }
  }
  const keepGraphMount = view.view === 'graph' ? root.querySelector<HTMLElement>('[data-graph-mount]') : null;
  const liveListScroll = root.querySelector<HTMLElement>('.wiki-list-scroll');
  if (liveListScroll && listScrollMemory) listScrollMemory.top = liveListScroll.scrollTop;
  root.innerHTML = `
    <div class="page-shell page-shell--wiki${wikiBrowserOpen ? '' : ' wiki-browser-collapsed'}">
      <header class="page-header page-header--hub">
        <div class="page-header__copy">
          <h1 class="page-header__title">Wiki <span class="accent">知识库</span></h1>
          <p class="page-header__desc">都什么年代了，还在古法记笔记？？</p>
        </div>
        <div class="page-header__actions">
          <select id="wiki-kb-select" class="wiki-kb-select" title="选择知识库" aria-label="选择知识库"${view.kbs.length === 0 ? ' disabled' : ''}>${kbOptions}</select>
          <button type="button" class="hub-refresh-btn" data-kb-create-toggle title="新建知识库" aria-label="新建知识库">${uiIcon('icon-plus')}</button>
          <button type="button" class="hub-refresh-btn" data-kb-delete title="删除当前知识库、原始素材及专属 Wiki 问答历史（内置知识库不可删）" aria-label="删除知识库"${!view.kbId || view.kbId === DEFAULT_KB_ID || view.kbId === TUTORIAL_KB_ID ? ' disabled' : ''}>${uiIcon('icon-trash')}</button>
          <button type="button" class="hub-refresh-btn" data-upload title="上传文件到知识库" aria-label="上传文件"${uploadDisabled ? ' disabled' : ''}>${uiIcon('icon-upload')}</button>
          ${batchToggle}
          <button type="button" class="hub-refresh-btn" data-wiki-browser-toggle title="收起/展开知识库面板" aria-label="知识库面板" aria-pressed="${wikiBrowserOpen}">${uiIcon('icon-panel-right')}</button>
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
      placeholder.replaceWith(keepAgentPanel);
      agentPanelKept = true;
      const messagesEl = keepAgentPanel.querySelector<HTMLElement>('[data-wiki-agent-messages]');
      if (messagesEl && agentMessagesScrollTop > 0) messagesEl.scrollTop = agentMessagesScrollTop;
    }
  }
  // 各组占位节点随 innerHTML 重新生成，签名未变的组子树换回复用。
  const keptGroupIds = new Set<string>();
  for (const [groupId, liveGroup] of keepGroups) {
    const placeholder = root.querySelector<HTMLElement>(`[data-wiki-group="${groupId}"]`);
    if (placeholder) {
      placeholder.replaceWith(liveGroup);
      keptGroupIds.add(groupId);
    }
  }
  // 聚焦组不在签名内：被保留的子树可能带着旧的 is-focused，按当前 activeGroupId 同步一次。
  root.querySelectorAll('.wiki-detail-group').forEach((el) => {
    el.classList.toggle('is-focused', el.getAttribute('data-wiki-group') === view.activeGroupId);
  });
  if (keepGraphMount) root.querySelector<HTMLElement>('[data-graph-mount]')?.replaceWith(keepGraphMount);
  const listScrollKey = `${view.kbId ?? ''}:${view.view}`;
  const listScroll = root.querySelector<HTMLElement>('.wiki-list-scroll');
  if (listScroll && listScrollMemory?.key === listScrollKey) listScroll.scrollTop = listScrollMemory.top;
  listScrollMemory = { key: listScrollKey, top: listScroll?.scrollTop ?? 0 };
  bindEvents();
  // Wiki 页面使用常驻 TipTap 编辑器；Home/index 是系统文档，继续只读增量渲染。按组挂载/补挂。
  const mountGroupEditor = (group: WikiDetailGroup, target: HTMLElement, page: WikiPage): void => {
    const detail = groupDetail(group.id);
    detail.editor?.destroy();
    detail.editor = mountWikiEditor({
      element: target,
      markdown: page.content || '',
      onChange: () => scheduleWikiPageSave(group.id, page.id),
      onWikiLink: (title) => void resolveAndOpenWikiPage(title),
    });
  };
  for (const group of view.detailGroups) {
    const section = root.querySelector<HTMLElement>(`[data-wiki-group="${group.id}"]`);
    if (!section) continue;
    if (!keptGroupIds.has(group.id)) {
      // 子树被重建：先释放旧实例（防 observer/编辑器泄漏），再按组内当前选中挂载。
      disposeGroupDetail(group.id);
      if (group.selectedId) {
        const page = view.pageDetails[group.selectedId] ?? view.pages.find((p) => p.id === group.selectedId);
        const target = section.querySelector<HTMLElement>('[data-wiki-editor]');
        if (target && page?.content !== undefined) mountGroupEditor(group, target, page);
      } else if (group.selectedDocumentName) {
        const doc = view.vaultDocuments[group.selectedDocumentName];
        const target = section.querySelector<HTMLElement>('[data-wiki-fold-content]');
        if (target && doc?.content) {
          groupDetail(group.id).fold = mountFoldedMarkdown(target, doc.content);
          if (doc.name === 'Home.md') decorateHomeQuestions(target);
        }
      }
    } else if (group.selectedId) {
      // 子树被保留但编辑器挂载点为空（如上次渲染时正文未就绪）：补挂。
      const target = section.querySelector<HTMLElement>('[data-wiki-editor]');
      const page = view.pageDetails[group.selectedId] ?? view.pages.find((item) => item.id === group.selectedId);
      if (target && page?.content !== undefined && target.childElementCount === 0) {
        mountGroupEditor(group, target, page);
      }
    }
  }
  // 无 KB 空态分支不渲染详情组：清空签名，下次重建全部重来。
  const groupsRendered = !!root.querySelector('.wiki-detail-groups');
  lastGroupsKey = groupsRendered ? groupsKeyNow : null;
  lastDetailSigs.clear();
  if (groupsRendered) {
    for (const [groupId, sig] of detailSigNowByGroup) lastDetailSigs.set(groupId, sig);
  }
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
        getSelectedId: () => activeGroup().selectedId,
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
    for (const group of view.detailGroups) {
      if (group.selectedId && !view.pages.some((p) => p.id === group.selectedId)) {
        group.selectedId = null;
      }
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
 * 选中页面并展示详情（列表条目 / 树节点 / 相关页链接 / 图谱节点共用），默认落在聚焦组。
 * 同一页面全局只能存在于一个组：已在别的组打开时聚焦该组该 tab，而不是重复打开。
 * 树视图按需展开祖先目录；本地无完整正文时直接进加载态拉取（brief 条目无正文；
 * 图谱节点可能不在已分页加载的列表里），避免先闪一帧空正文再切加载文案。
 */
function selectWikiPage(pageId: string, opts?: { expandTree?: boolean; groupId?: string }): void {
  const target = (opts?.groupId ? groupById(opts.groupId) : activeGroup()) ?? view.detailGroups[0];
  if (!target) return;
  const key = `page:${pageId}`;
  const owner = findTabOwner(key);
  if (owner && owner.id !== target.id) {
    // 已在另一组打开：聚焦该组该 tab（全局唯一约束）。
    view.activeGroupId = owner.id;
    const ownedTab = owner.tabs.find((t) => tabKey(t) === key);
    if (ownedTab && groupActiveKey(owner) !== key) activateTabInGroup(owner, ownedTab);
    else renderShell();
    return;
  }
  // 从 vault 文档（Home.md/index.md）切回页面：清掉组内文档态，否则 detailHtml 仍优先显示文档。
  // 图谱节点点击走 onSelectPage 直接调这里，不经过列表条目的清理逻辑，必须在这里兜底。
  const hadDocument = target.selectedDocumentName !== null;
  target.selectedDocumentName = null;
  target.tabs = openTabUnique(target.tabs, { kind: 'page', id: pageId });
  view.activeGroupId = target.id;
  if (pageId === target.selectedId) {
    // 已选中但详情停在文档态时，重渲染让详情切回页面。
    if (hadDocument) renderShell();
    return;
  }
  const previousId = target.selectedId;
  if (previousId) flushGroupDirty(target);
  target.selectedId = pageId;
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

async function loadVaultDocument(name: 'Home.md' | 'index.md', opts?: { groupId?: string }): Promise<void> {
  if (!view.kbId) return;
  const target = (opts?.groupId ? groupById(opts.groupId) : activeGroup()) ?? view.detailGroups[0];
  if (!target) return;
  const key = `doc:${name}`;
  const owner = findTabOwner(key);
  if (owner && owner.id !== target.id) {
    // 已在另一组打开：聚焦该组该 tab（全局唯一约束）。
    view.activeGroupId = owner.id;
    const ownedTab = owner.tabs.find((t) => tabKey(t) === key);
    if (ownedTab && groupActiveKey(owner) !== key) activateTabInGroup(owner, ownedTab);
    else renderShell();
    return;
  }
  const seq = loadSeq;
  target.selectedId = null;
  target.selectedDocumentName = name;
  target.tabs = openTabUnique(target.tabs, { kind: 'doc', name });
  view.activeGroupId = target.id;
  loadingVaultDocs.add(name);
  renderShell();
  try {
    const res = await backendApi.wikiVaultDocument(name, view.kbId);
    if (seq !== loadSeq) return;
    view.vaultDocuments = { ...view.vaultDocuments, [name]: res.document };
  } catch (err) {
    if (seq === loadSeq) {
      notify(`加载 ${name} 失败：${(err as Error).message}`);
      for (const group of view.detailGroups) {
        if (group.selectedDocumentName === name) group.selectedDocumentName = null;
      }
    }
  } finally {
    if (seq === loadSeq) loadingVaultDocs.delete(name);
  }
  renderShell();
}

/** 激活组内某个 tab（tab 点击 / 拆分移动后落焦点共用）；页面走 selectWikiPage，文档走 loadVaultDocument。 */
function activateTabInGroup(group: WikiDetailGroup, tab: WikiOpenTab): void {
  view.activeGroupId = group.id;
  if (tab.kind === 'page') {
    selectWikiPage(tab.id, { groupId: group.id });
  } else {
    // 切到只读 vault 文档前 flush 脏编辑（loadVaultDocument 自身不做，对齐 selectWikiPage 的行为）。
    flushGroupDirty(group);
    void loadVaultDocument(tab.name, { groupId: group.id });
  }
}

/**
 * 跨组移动 tab：源组移除（正编辑则先 flush 脏稿，源组切到相邻 tab），目标组接收并激活。
 */
function moveTabToGroup(key: string, targetGroupId: string): void {
  const source = findTabOwner(key);
  const target = groupById(targetGroupId);
  if (!source || !target || source.id === target.id) return;
  const tab = source.tabs.find((t) => tabKey(t) === key);
  if (!tab) return;
  if (groupActiveKey(source) === key) flushGroupDirty(source);
  const wasActive = groupActiveKey(source) === key;
  const { tabs, nextActiveKey } = closeTabByKey(source.tabs, key, groupActiveKey(source));
  source.tabs = tabs;
  target.tabs = openTabUnique(target.tabs, tab);
  if (wasActive) {
    // 源组切到相邻 tab（无相邻则回空态）；焦点随后落到目标组。
    const neighbor = source.tabs.find((t) => tabKey(t) === nextActiveKey);
    if (neighbor) {
      activateTabInGroup(source, neighbor);
    } else {
      source.selectedId = null;
      source.selectedDocumentName = null;
      disposeGroupDetail(source.id);
    }
  }
  activateTabInGroup(target, tab);
}

/**
 * 把 tab 按方向拆到第二组：单组时创建第二组（方向记入 groupOrientation）并迁入，
 * 拆的是组内唯一 tab 时源组变空态（允许空组存在，对齐 VSCode）；已有两组时退化为移到另一组。
 */
function splitGroupToOrientation(key: string, orientation: 'row' | 'column'): void {
  const source = findTabOwner(key);
  if (!source) return;
  if (view.detailGroups.length >= 2) {
    const other = view.detailGroups.find((g) => g.id !== source.id);
    if (other) moveTabToGroup(key, other.id);
    return;
  }
  view.groupOrientation = orientation;
  const next = createDetailGroup();
  view.detailGroups = [...view.detailGroups, next];
  moveTabToGroup(key, next.id);
}

/** 回收空组（两组时某组最后一个 tab 被关掉）：释放编辑器/计时器，剩余组恢复通栏。 */
function collapseGroup(groupId: string): void {
  disposeGroupDetail(groupId);
  lastDetailSigs.delete(groupId);
  view.detailGroups = view.detailGroups.filter((g) => g.id !== groupId);
  if (view.detailGroups.length === 0) view.detailGroups = [createDetailGroup()];
  view.activeGroupId = view.detailGroups[0].id;
  renderShell();
}

/**
 * 关闭某组内的一个 tab。关闭激活 tab 时切到相邻 tab（右侧优先）；
 * 有未保存编辑时先弹确认（确认即保存后关闭），关闭非激活 tab 只更新 tab 栏；
 * 两组时关掉某组最后一个 tab 直接回收该组。
 */
async function closeWikiTab(key: string): Promise<void> {
  const group = findTabOwner(key);
  if (!group) return;
  view.activeGroupId = group.id;
  const activeKey = groupActiveKey(group);
  const closingActive = activeKey !== null && key === activeKey;
  if (closingActive && group.selectedId && groupDetails.get(group.id)?.dirty) {
    const confirmed = await showConfirmDialog({
      title: '关闭页面',
      message: '当前页面有未保存的修改，关闭前需要先保存。',
      confirmText: '保存并关闭',
    });
    if (!confirmed) return;
    await saveWikiPageDraft(group.id, group.selectedId);
  }
  const { tabs, nextActiveKey } = closeTabByKey(group.tabs, key, activeKey);
  group.tabs = tabs;
  if (tabs.length === 0 && view.detailGroups.length > 1) {
    collapseGroup(group.id);
    return;
  }
  if (!closingActive) {
    // 关闭非激活 tab：编辑器与当前详情不动，仅 tab 栏重渲染。
    renderShell();
    return;
  }
  const next = tabs.find((t) => tabKey(t) === nextActiveKey);
  if (next) {
    activateTabInGroup(group, next);
    return;
  }
  // 组内最后一个 tab 被关掉（单组）：回到空态。
  group.selectedId = null;
  group.selectedDocumentName = null;
  disposeGroupDetail(group.id);
  renderShell();
}

// ── Tab 拖拽（拖到另一组 = 移动；拖到详情区四边缘 = 按方向拆分） ──

/** 拖拽落点：group:<id> = 移到该组；edge:left|right = row 拆分，edge:top|bottom = column 拆分。 */
export type WikiDropZone = `group:${string}` | 'edge:left' | 'edge:right' | 'edge:top' | 'edge:bottom';

/** 进行中的 tab 拖拽（模块级变量，不用 dataTransfer 存复杂数据）。 */
let tabDrag: { tabKey: string; sourceGroupId: string } | null = null;
/** 当前高亮的落点（dragover 命中；dragleave/dragend/drop 清除）。 */
let currentDropZone: WikiDropZone | null = null;

/** drop 核心逻辑（纯状态操作，可单测）：事件处理器只做 zone 判定后调用它。 */
export function resolveTabDrop(tabKey: string, sourceGroupId: string, zone: WikiDropZone): void {
  if (zone.startsWith('group:')) {
    const targetGroupId = zone.slice('group:'.length);
    if (targetGroupId !== sourceGroupId) moveTabToGroup(tabKey, targetGroupId);
    return;
  }
  const edge = zone.slice('edge:'.length);
  splitGroupToOrientation(tabKey, edge === 'left' || edge === 'right' ? 'row' : 'column');
}

/** 由鼠标坐标判定落点：两组时命中某组主体即移动落点；单组时四边缘区（各 25%）为拆分落点，中心区为同组 no-op。 */
function dropZoneAtPoint(container: HTMLElement, x: number, y: number): WikiDropZone | null {
  const sections = Array.from(container.querySelectorAll<HTMLElement>('[data-wiki-group]'));
  if (sections.length === 0) return null;
  if (sections.length === 2) {
    for (const section of sections) {
      const rect = section.getBoundingClientRect();
      if (x >= rect.left && x <= rect.right && y >= rect.top && y <= rect.bottom) {
        return `group:${section.getAttribute('data-wiki-group') ?? ''}`;
      }
    }
    return null;
  }
  const onlyGroupId = sections[0].getAttribute('data-wiki-group') ?? '';
  const rect = container.getBoundingClientRect();
  // 测试环境（happy-dom）rect 恒 0：无法判定边缘，按同组落点处理（no-op）。
  if (rect.width <= 0 || rect.height <= 0) return `group:${onlyGroupId}`;
  const relX = (x - rect.left) / rect.width;
  const relY = (y - rect.top) / rect.height;
  if (relX < 0.25) return 'edge:left';
  if (relX > 0.75) return 'edge:right';
  if (relY < 0.25) return 'edge:top';
  if (relY > 0.75) return 'edge:bottom';
  return `group:${onlyGroupId}`;
}

/** 落点高亮：组落点加 is-drop-target，边缘落点在容器上标 data-drop-edge（CSS 画 overlay）。 */
function highlightDropZone(container: HTMLElement, zone: WikiDropZone | null): void {
  if (zone === currentDropZone) return;
  clearDropZoneHighlight(container);
  currentDropZone = zone;
  if (!zone) return;
  if (zone.startsWith('group:')) {
    container
      .querySelector<HTMLElement>(`[data-wiki-group="${zone.slice('group:'.length)}"]`)
      ?.classList.add('is-drop-target');
  } else {
    container.dataset.dropEdge = zone.slice('edge:'.length);
  }
}

function clearDropZoneHighlight(container: HTMLElement): void {
  currentDropZone = null;
  delete container.dataset.dropEdge;
  container.querySelectorAll('.is-drop-target').forEach((el) => el.classList.remove('is-drop-target'));
}

/** KB 概览只用于详情空态展示，失败静默（不打扰主流程）。 */
async function loadKbSummary(): Promise<void> {
  if (!view.kbId) return;
  const seq = loadSeq;
  try {
    const res = await backendApi.wikiSummary(view.kbId);
    if (seq !== loadSeq) return;
    view.kbSummary = res.status === 'ready' && res.summary ? { summary: res.summary, page_count: res.page_count, source_count: res.source_count, generated_at: res.generated_at, status: res.status } : null;
    if (!activeGroup().selectedId) renderShell();
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
  // 编辑器组重置为单个空组：旧组运行时装（编辑器/计时器）全部释放。
  for (const group of view.detailGroups) disposeGroupDetail(group.id);
  const detailGroup = createDetailGroup();
  view.detailGroups = [detailGroup];
  view.activeGroupId = detailGroup.id;
  view.groupOrientation = 'row';
  view.vaultDocuments = {};
  view.pageDetails = {};
  view.sourcePages = {};
  view.relationPages = {};
  view.kbSummary = null;
  view.expandedPaths = new Set<string>(DEFAULT_EXPANDED_PATHS);
  loadingDetails.clear();
  loadingVaultDocs.clear();
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

// 文件附件统一从对话区 Composer 进入 Wiki Agent 工作流；页面不再编排上传或 ingest。
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

// ── 单条操作（重命名 / 删除，Phase 2） ──

/** 删除后重新加载列表与概览（互不依赖，并行）；删的是某组当前选中页时清空该组详情栏。 */
async function refreshAfterDelete(deletedIds: string[]): Promise<void> {
  for (const group of view.detailGroups) {
    if (group.selectedId && deletedIds.includes(group.selectedId)) {
      group.selectedId = null;
    }
  }
  await Promise.all([loadPages(), loadKbSummary()]);
}

/** 重命名：应用内输入弹窗（Electron 不支持 window.prompt），只更新 title，后端缺省字段沿用旧值。 */
async function handleRenamePage(page: WikiPage): Promise<void> {
  const next = await showPromptDialog({ title: '重命名页面', defaultValue: page.title });
  const title = next?.trim();
  if (!title || title === page.title) return;
  // 正打开该页且有未保存草稿时先落盘，避免 loadPages 的重渲染把草稿冲掉（同 selectWikiPage 的切换保存）。
  const owner = findTabOwner(`page:${page.id}`);
  if (owner && owner.selectedId === page.id) flushGroupDirty(owner);
  try {
    const result = await backendApi.wikiUpdatePage(page.id, { title }, view.kbId ?? undefined);
    notify('已重命名页面');
    // 详情缓存同步新标题（若正打开该页），列表刷新与删除后一致走 loadPages。
    const detail = view.pageDetails[page.id];
    if (detail) view.pageDetails = { ...view.pageDetails, [page.id]: { ...detail, title: result.page.title } };
    await loadPages();
  } catch (err) {
    notify(`重命名失败：${errMsg(err)}`);
  }
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

/** 调主进程用系统默认程序打开 Wiki 来源的原始文件（docx → Word/WPS 等）。 */
async function openWikiSourceFile(sourceId: string, kbId?: string): Promise<void> {
  if (!window.Crew?.openWikiSourceFile) {
    notify('当前环境不支持打开原始文件');
    return;
  }
  try {
    await window.Crew.openWikiSourceFile(sourceId, kbId);
  } catch (error) {
    notify(`打开原始文件失败：${error instanceof Error ? error.message : String(error)}`);
  }
}

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

  onClick('[data-page-menu]', (btn, e) => {
    // ⋯ 菜单按钮在条目内部，阻止冒泡避免触发选中/打开详情。
    e.stopPropagation();
    const id = btn.getAttribute('data-page-menu') ?? '';
    const page = view.pages.find((p) => p.id === id);
    if (!page) return;
    showContextMenu(btn, [
      {
        id: 'open',
        label: '打开',
        onSelect: () => selectWikiPage(page.id, { expandTree: true }),
      },
      {
        id: 'rename',
        label: '重命名',
        onSelect: () => void handleRenamePage(page),
      },
      {
        id: 'delete',
        label: '删除',
        danger: true,
        onSelect: () => void handleDeletePage(page.id, page.title),
      },
    ]);
  });
  onClick('[data-kb-create-toggle]', () => {
    kbCreateOpen = !kbCreateOpen;
    renderShell();
    if (kbCreateOpen) {
      root.querySelector<HTMLInputElement>('[data-kb-create-input]')?.focus();
    }
  });
  onClick('[data-wiki-tour]', () => startWikiTour());
  onClick('[data-wiki-browser-toggle]', () => toggleWikiBrowser());
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

  // ── 知识库面板把手（在面板左缘，sign=-1）：列表/树/类型视图调 browserWidth；图谱视图调 graphWidth（null=沿用 browserWidth） ──
  $$w('[data-wiki-browser-sash]').forEach((sash) => {
    const pane = root.querySelector<HTMLElement>('.wiki-browser-pane');
    if (!pane) return;
    bindPaneSash(sash, {
      sign: -1, // 把手在面板左缘：向左拖变宽、向右拖变窄
      // 图谱模式 browserWidth 不反映当前宽度（可能已被 graphWidth 覆盖），取实际测量值
      startWidth: () => (view.view === 'graph' ? pane.getBoundingClientRect().width : browserWidth),
      onDrag: (w) => {
        if (view.view === 'graph') {
          graphWidth = graphWidthStore.clamp(w);
          pane.style.width = `${graphWidth}px`;
        } else {
          browserWidth = browserWidthStore.clamp(w);
          pane.style.width = `${browserWidth}px`;
        }
      },
      onCommit: () => (view.view === 'graph' ? graphWidthStore.persist(graphWidth) : browserWidthStore.persist(browserWidth)),
      // 双击复位：列表回默认宽度；图谱清掉拖拽固定像素，回退到 browserWidth
      onReset: () => {
        if (view.view === 'graph') {
          graphWidth = null;
          graphWidthStore.persist(null);
        } else {
          browserWidth = WIKI_BROWSER_DEFAULT_WIDTH;
          browserWidthStore.persist(browserWidth);
        }
        renderShell();
      },
    });
  });

  // ── 目录内层把手（在目录右缘，sign=+1）：往右拖目录变宽；仅列表/树/类型视图渲染（图谱模式目录走比例分配） ──
  $$w('[data-wiki-catalog-sash]').forEach((sash) => {
    const pane = root.querySelector<HTMLElement>('.wiki-list-pane');
    if (!pane) return;
    bindPaneSash(sash, {
      startWidth: () => catalogWidth,
      onDrag: (w) => {
        catalogWidth = catalogWidthStore.clamp(w);
        pane.style.flex = `0 0 ${catalogWidth}px`;
      },
      onCommit: () => catalogWidthStore.persist(catalogWidth),
      // 双击复位默认目录宽度
      onReset: () => {
        catalogWidth = WIKI_CATALOG_DEFAULT_WIDTH;
        catalogWidthStore.persist(catalogWidth);
        renderShell();
      },
    });
  });

  // ── 图谱画布内层把手（在画布右缘，sign=+1）：往右拖画布变宽；仅图谱视图渲染，双击清除固定像素恢复 1.5:1 弹性比例 ──
  $$w('[data-wiki-graph-canvas-sash]').forEach((sash) => {
    const pane = root.querySelector<HTMLElement>('.wiki-list-pane');
    if (!pane) return;
    bindPaneSash(sash, {
      // 未拖拽时画布是弹性宽度（graphCanvasWidth 为 null），起始宽度取实际测量值
      startWidth: () => graphCanvasWidth ?? pane.getBoundingClientRect().width,
      onDrag: (w) => {
        graphCanvasWidth = graphCanvasWidthStore.clamp(w);
        pane.style.flex = `0 0 ${graphCanvasWidth}px`;
      },
      onCommit: () => graphCanvasWidthStore.persist(graphCanvasWidth),
      // 双击复位：清掉拖拽固定像素，回到 1.5:1 弹性分配
      onReset: () => {
        graphCanvasWidth = null;
        graphCanvasWidthStore.persist(null);
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
    selectWikiPage(id, { expandTree: true });
  });

  $$w<HTMLInputElement>('[data-wiki-title]').forEach((input) => {
    const groupId = input.closest('[data-wiki-group]')?.getAttribute('data-wiki-group') ?? '';
    input.addEventListener('input', () => {
      const group = groupById(groupId);
      if (group?.selectedId) scheduleWikiPageSave(group.id, group.selectedId);
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
    selectWikiPage(pageId, { expandTree: true });
  });
  onClick('[data-related-page-id]', (item) => {
    const pageId = item.getAttribute('data-related-page-id');
    if (!pageId) return;
    selectWikiPage(pageId, { expandTree: true });
  });

  onClick('[data-vault-document]', (item) => {
    const name = item.getAttribute('data-vault-document');
    if (name !== 'Home.md' && name !== 'index.md') return;
    const group = activeGroup();
    if (name === group.selectedDocumentName && view.vaultDocuments[name]) return;
    void loadVaultDocument(name);
  });

  // ── 详情编辑器组：tab 激活/关闭/右键拆分菜单 + 点击组内更新聚焦组 ──
  $$w('.wiki-detail-group').forEach((section) => {
    // mousedown 即切换聚焦组（先于 focus/click），只改 class 不重渲染，避免打扰编辑。
    section.addEventListener('mousedown', () => {
      const groupId = section.getAttribute('data-wiki-group') ?? '';
      if (!groupId || view.activeGroupId === groupId) return;
      view.activeGroupId = groupId;
      $$w('.wiki-detail-group').forEach((el) => el.classList.toggle('is-focused', el === section));
    });
  });
  onClick('[data-wiki-tab]', (btn) => {
    const key = btn.getAttribute('data-wiki-tab') ?? '';
    const group = groupById(btn.closest('[data-wiki-group]')?.getAttribute('data-wiki-group') ?? '');
    if (!key || !group) return;
    if (key === groupActiveKey(group)) {
      view.activeGroupId = group.id;
      return;
    }
    const tab = group.tabs.find((t) => tabKey(t) === key);
    if (!tab) return;
    activateTabInGroup(group, tab);
  });
  onClick('[data-wiki-tab-close]', (btn, e) => {
    // 关闭按钮在 chip 内部，阻止冒泡避免触发激活。
    e.stopPropagation();
    const key = btn.getAttribute('data-wiki-tab-close') ?? '';
    if (key) void closeWikiTab(key);
  });
  $$w('[data-wiki-tab-chip]').forEach((chip) => {
    // tab 右键菜单：单组时「向右拆分」「向下拆分」；两组时「移到另一组」。
    chip.addEventListener('contextmenu', (e) => {
      e.preventDefault();
      const key = chip.getAttribute('data-wiki-tab-chip') ?? '';
      const owner = key ? findTabOwner(key) : null;
      if (!key || !owner) return;
      const items: ContextMenuItem[] =
        view.detailGroups.length < 2
          ? [
              { id: 'split-right', label: '向右拆分', onSelect: () => splitGroupToOrientation(key, 'row') },
              { id: 'split-down', label: '向下拆分', onSelect: () => splitGroupToOrientation(key, 'column') },
            ]
          : [
              {
                id: 'move-to-other',
                label: '移到另一组',
                onSelect: () => {
                  const other = view.detailGroups.find((g) => g.id !== owner.id);
                  if (other) moveTabToGroup(key, other.id);
                },
              },
            ];
      showContextMenu(chip, items);
    });
    // tab 拖拽：dragstart 记录 { tabKey, sourceGroupId }（模块级变量），落点由详情区 dragover/drop 判定。
    chip.addEventListener('dragstart', (e) => {
      const key = chip.getAttribute('data-wiki-tab-chip') ?? '';
      const groupId = chip.closest('[data-wiki-group]')?.getAttribute('data-wiki-group') ?? '';
      if (!key || !groupId) return;
      tabDrag = { tabKey: key, sourceGroupId: groupId };
      (e as DragEvent).dataTransfer?.setData('text/plain', key);
    });
    chip.addEventListener('dragend', () => {
      tabDrag = null;
      const container = root.querySelector<HTMLElement>('.wiki-detail-groups');
      if (container) clearDropZoneHighlight(container);
    });
  });

  // 拖拽经过详情区：dragover 判定落点并高亮（两组时只有组落点；单组时四边缘为拆分落点）。
  const detailGroupsEl = root.querySelector<HTMLElement>('.wiki-detail-groups');
  if (detailGroupsEl) {
    detailGroupsEl.addEventListener('dragover', (e) => {
      if (!tabDrag) return;
      e.preventDefault();
      highlightDropZone(detailGroupsEl, dropZoneAtPoint(detailGroupsEl, e.clientX, e.clientY));
    });
    detailGroupsEl.addEventListener('dragleave', (e) => {
      const related = (e as DragEvent).relatedTarget as Node | null;
      if (!related || !detailGroupsEl.contains(related)) clearDropZoneHighlight(detailGroupsEl);
    });
    detailGroupsEl.addEventListener('drop', (e) => {
      if (!tabDrag) return;
      e.preventDefault();
      const zone = dropZoneAtPoint(detailGroupsEl, e.clientX, e.clientY);
      const drag = tabDrag;
      tabDrag = null;
      clearDropZoneHighlight(detailGroupsEl);
      if (zone) resolveTabDrop(drag.tabKey, drag.sourceGroupId, zone);
    });
  }

  // ── 组间把手（两组时渲染）：row 方向调宽度比，column 方向调高度比；比例持久化，双击复位 50% ──
  $$w('[data-wiki-group-sash]').forEach((sash) => {
    const container = root.querySelector<HTMLElement>('.wiki-detail-groups');
    const sections = container?.querySelectorAll<HTMLElement>('.wiki-detail-group');
    const first = sections?.[0];
    const second = sections?.[1];
    if (!container || !first || !second) return;
    const axis = view.groupOrientation === 'column' ? 'y' : 'x';
    // 容器尺寸换算「像素↔百分比」；happy-dom 等 rect 为 0 的环境回落 1000px 假定尺寸。
    const containerSize = (): number =>
      (axis === 'y' ? container.getBoundingClientRect().height : container.getBoundingClientRect().width) || 1000;
    bindPaneSash(sash, {
      axis,
      startWidth: () => (groupSplitRatio / 100) * containerSize(),
      onDrag: (w) => {
        groupSplitRatio = groupSplitStore.clamp((w / containerSize()) * 100);
        first.style.flex = `${groupSplitRatio} 1 0`;
        second.style.flex = `${100 - groupSplitRatio} 1 0`;
      },
      onCommit: () => groupSplitStore.persist(groupSplitRatio),
      // 双击复位 50%（清存储，回默认）；直接改内联比例，不走 renderShell（保活子树会带回旧内联值）。
      onReset: () => {
        groupSplitRatio = 50;
        groupSplitStore.persist(null);
        first.style.flex = '50 1 0';
        second.style.flex = '50 1 0';
      },
    });
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
      // 「打开原始文件」链接（/api/wiki/sources/{id}/file）：web 端相对地址
      // 指向 gateway 可直接打开；桌面端渲染进程够不到 gateway，拦截后交给
      // 主进程查询原始路径并用系统默认程序打开。
      const fileLink = (e.target as HTMLElement).closest('a[href*="/api/wiki/sources/"]') as HTMLAnchorElement | null;
      if (fileLink) {
        const href = fileLink.getAttribute('href') ?? '';
        const match = href.match(/\/api\/wiki\/sources\/([^/?]+)\/file(?:\?|$)/);
        if (match) {
          e.preventDefault();
          const kbFromHref = new URLSearchParams(href.split('?')[1] ?? '').get('kb_id');
          void openWikiSourceFile(decodeURIComponent(match[1]), kbFromHref || undefined);
          return;
        }
      }
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
