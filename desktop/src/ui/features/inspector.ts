/**
 * 聊天右侧 Inspector 面板（codex / opencode 风格）
 *
 * Tab：Context / Files / Plan / Kanban / 协作（仅 Team Session）/ Browser
 *
 * 关键：
 *   - Context Tab 末尾加"原始消息"列表（user/assistant 完整 JSON + messageID + 时间）
 *     这是 codex/opencode 都有的核心信息
 *   - 上下文字段显示：会话信息 + 用量进度条 + 输入/输出 token + 缓存
 *     + 构成饼图 + 成本 + 系统提示 + **原始消息 JSON**
 *
 * 数据来源：当前会话（state.messages / state.config / state.backendSessions）。
 * 当后端未返回真实 token 计数时，按字符长度 4 字符 ≈ 1 token 的近似值推算，
 * 让 UI 在没有 usage 接口的情况下也能显示有意义的数据；
 * 后端如提供 usage 接口，调用 setUsageSnapshot 覆盖即可。
 */

import DOMPurify from 'dompurify';
import { backendApi } from '../backend-client';
import { $, escapeHtml, state, getBookTodos, getBookFileChanges, isDynamicKanbanSession, notify } from '../state';
import { sessionStore } from '../stores/stores';
import type { ChatMessage, PlanReviewStatus } from '../chat-render';
import type { TodoItem } from '../state';
import {
  activateKanbanInspectorTab,
  buildKanbanInspectorHtml,
  disconnectKanbanObserver,
} from './kanban-board';
import {
  activateTeamCollaborationBoard,
  buildTeamCollaborationBoardHtml,
  refreshTeamCollaborationBoard,
  startTeamCollaborationPolling,
  stopTeamCollaborationPolling,
  teamCollaborationTaskCount,
} from './team-collaboration-board';
import { isStudioView } from './studio-chrome-state';
import { setRuntimeStyle, setRuntimeToken } from '../components/runtime-style';
import {
  findModelOption,
  modelLabelForId,
  resolveSessionModelId,
  resolveSessionModelWindow,
  isExternalTeamSession,
} from './session-model';
import { renderDiffPanelHtml, applyDiffSyntaxHighlights, buildDiffFromTexts, countDiffRows, expandCollapsedRegion, clearDiffExpandsForPath, type BackendDiffRow, type DiffRegionExpandMap } from '../diff-lines';
import { isPlanDocumentPath } from '../plan-document-path';
import { renderMarkdownHtml } from '../markdown';
import {
  buildOfflinePreviewDocument,
  filePreviewKind,
  isBinaryPreviewKind,
  isTextPreviewKind,
  type FilePreviewKind,
} from '../file-preview';
import {
  bindBrowserPanel,
  hideBrowserPanelView,
  openUserBrowser,
  releaseUserBrowserControl,
  renderBrowserPanel,
  syncBrowserPanelSession,
} from './browser-panel';
import {
  commitDraftSession,
  composerWorkspaceId,
  ensureComposerDraftSession,
} from './workspaces';
import {
  mountInspectorShell,
  type InspectorTabKey,
} from '../layouts/inspector-shell';
import { showFileOpenMenu } from './file-open-menu';
import { InspectorSessionUiStore } from './inspector-context-files';
import {
  createPlanBoardUiState,
  isPlanActionable,
  planStatusLabel,
  syncPlanBoardUiSession,
} from './inspector-workflow';

type TabKey = InspectorTabKey;

interface FileChange {
  path: string;
  name: string;
  added: number;
  removed: number;
  status: 'modified' | 'added' | 'deleted';
  diff: DiffRow[];
}

type InspectorFileSummary = {
  path: string;
  name?: string;
  added?: number;
  removed?: number;
  status?: 'modified' | 'added' | 'deleted';
  diff?: DiffRow[];
};

type DiffRow = BackendDiffRow;

interface ContextStats {
  provider: string;
  model: string;
  modelId: string;
  variant: string;
  sessionId: string;
  startedAt: string;
  lastActiveAt: string;
  contextWindow: number;
  usedTokens: number;
  inputTokens: number;
  outputTokens: number;
  cacheRead: number;
  cacheWrite: number;
  workingDir: string;
  systemPrompt: string;
  breakdown: { label: string; tokens: number; tone: 'user' | 'assistant' | 'tool' | 'system' | 'other' }[];
}

interface PlanStep {
  id: string;
  name: string;
  status: 'done' | 'running' | 'waiting';
  meta?: string;
}

interface OriginalMessage {
  id: string;
  sessionId: string;
  role: 'user' | 'assistant' | 'tool';
  type: 'text' | 'function_call' | 'function_response';
  time: string;
  text: string;
}

interface UsageSnapshot {
  promptTokens?: number | undefined;
  completionTokens?: number | undefined;
  cacheRead?: number | undefined;
  cacheWrite?: number | undefined;
  promptBreakdown?: { system?: number; reminder?: number; tools?: number } | undefined;
}

// ─── 真实数据计算 ─────────────────────────────────────────

/** 大致估算 token 数：英文 4 字符 ≈ 1 token，中文 1.5 字符 ≈ 1 token。 */
function estimateTokens(text: string): number {
  if (!text) return 0;
  let asciiChars = 0;
  let cjkChars = 0;
  for (const ch of text) {
    const code = ch.codePointAt(0) ?? 0;
    if (code < 0x80) asciiChars += 1;
    else if (code >= 0x4e00 && code <= 0x9fff) cjkChars += 1;
    else asciiChars += 0.5;
  }
  return Math.max(0, Math.round(asciiChars / 4 + cjkChars / 1.5));
}

function pad2(n: number): string { return String(n).padStart(2, '0'); }
function formatTimestamp(ts: number): string {
  if (!ts) return '';
  const d = new Date(ts);
  return `${d.getFullYear()}/${pad2(d.getMonth() + 1)}/${pad2(d.getDate())} ${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`;
}

const usageBySession = new Map<string, UsageSnapshot>();
const contextBySession = new Map<string, { used_tokens: number; max_tokens: number; ratio: number }>();

/** 从网关拉取会话真实上下文用量（/api/session/{id}/context）。 */
export async function loadInspectorContext(sessionId: string | null): Promise<void> {
  if (!sessionId) return;
  try {
    const ctx = await backendApi.sessionContext(sessionId);
    contextBySession.set(sessionId, ctx);
  } catch {
    contextBySession.delete(sessionId);
  }
  refreshInspector();
}

/** 外部（usage API / WebSocket）写入真实用量时调用，刷新右侧 Inspector。 */
export function setUsageSnapshot(sessionId: string, snap: UsageSnapshot): void {
  usageBySession.set(sessionId, snap);
  refreshInspector();
}

function getActiveMessages(): ChatMessage[] {
  const sid = state.activeSessionId;
  if (!sid) return [];
  return state.messages[sid] ?? [];
}

function getProviderFromConfig(modelId?: string): string {
  const id = modelId ?? resolveSessionModelId();
  const m = findModelOption(id);
  if (m?.base_url) {
    try {
      const host = new URL(m.base_url).hostname;
      return host || 'unknown';
    } catch { /* ignore */ }
  }
  return state.backendConnected ? 'gateway' : '—';
}

function getSessionStartedAt(): string {
  const list = getActiveMessages();
  if (list.length === 0) return '—';
  let minTs = Number.POSITIVE_INFINITY;
  for (const m of list) if (m.timestamp && m.timestamp < minTs) minTs = m.timestamp;
  if (!Number.isFinite(minTs)) return '—';
  return formatTimestamp(minTs).slice(0, 16);
}

function getLastActiveAt(): string {
  const list = getActiveMessages();
  if (list.length === 0) return '—';
  let maxTs = 0;
  for (const m of list) if (m.timestamp && m.timestamp > maxTs) maxTs = m.timestamp;
  if (!maxTs) return '—';
  return formatTimestamp(maxTs).slice(0, 16);
}

/** 计算当前会话的 ContextStats。无后端 usage 时按 4 字符 ≈ 1 token 估算。 */
function computeContextStats(): ContextStats {
  const messages = getActiveMessages();
  const modelId = resolveSessionModelId() || '—';
  const modelName = modelLabelForId(modelId);
  const provider = getProviderFromConfig(modelId);
  const contextWindow = resolveSessionModelWindow();
  const usage = state.activeSessionId ? usageBySession.get(state.activeSessionId) : undefined;
  const apiCtx = state.activeSessionId ? contextBySession.get(state.activeSessionId) : undefined;

  let userText = '';
  let assistantText = '';
  let toolText = '';
  let otherText = '';
  for (const m of messages) {
    if (m.role === 'user') {
      userText += (userText ? '\n' : '') + (m.content || '');
    } else if (m.role === 'assistant') {
      assistantText += (assistantText ? '\n' : '') + (m.content || '');
      if (m.thinking) otherText += (otherText ? '\n' : '') + m.thinking;
    } else if (m.role === 'status' || m.role === 'error') {
      otherText += (otherText ? '\n' : '') + (m.content || '');
    }
    if (m.toolCalls?.length) {
      for (const tc of m.toolCalls) {
        if (tc.args) toolText += (toolText ? '\n' : '') + tc.args;
        if (tc.result) toolText += (toolText ? '\n' : '') + tc.result;
      }
    }
  }

  const userTokens = estimateTokens(userText);
  const assistantTokens = estimateTokens(assistantText);
  const toolTokens = estimateTokens(toolText);
  const otherTokens = estimateTokens(otherText);

  const inputTokens = usage?.promptTokens ?? (userTokens + toolTokens + otherTokens);
  const outputTokens = usage?.completionTokens ?? assistantTokens;
  const usedTokens = apiCtx?.used_tokens
    ?? (usage?.promptTokens != null && usage?.completionTokens != null
      ? usage.promptTokens + usage.completionTokens
      : userTokens + assistantTokens + toolTokens + otherTokens);
  const cacheRead = usage?.cacheRead ?? 0;
  const cacheWrite = usage?.cacheWrite ?? 0;

  const variant = 'thinking';
  const sessionId = state.activeSessionId ?? '—';

  return {
    provider,
    model: modelName,
    modelId,
    variant,
    sessionId,
    startedAt: getSessionStartedAt(),
    lastActiveAt: getLastActiveAt(),
    contextWindow,
    usedTokens,
    inputTokens,
    outputTokens,
    cacheRead,
    cacheWrite,
    workingDir: '~/Projects/Crew',
    systemPrompt: '你是 Crew，一个面向日常工作的智能助手。你需要帮助用户完成信息整理、分析、协作与自动化任务，所有回复优先以中文给出。',
    breakdown: [
      { label: '用户', tokens: userTokens, tone: 'user' as const },
      { label: '助手', tokens: assistantTokens, tone: 'assistant' as const },
      { label: '工具调用', tokens: toolTokens, tone: 'tool' as const },
      ...(usage?.promptBreakdown?.system ? [{ label: '系统提示', tokens: usage.promptBreakdown.system, tone: 'system' as const }] : []),
      ...(usage?.promptBreakdown?.reminder ? [{ label: '技能·上下文', tokens: usage.promptBreakdown.reminder, tone: 'system' as const }] : []),
      ...(usage?.promptBreakdown?.tools ? [{ label: '工具定义', tokens: usage.promptBreakdown.tools, tone: 'system' as const }] : []),
      { label: '其他', tokens: otherTokens, tone: 'other' as const },
    ],
  };
}

/** 把当前会话消息格式化成 OriginalMessage 列表（用于原始消息 tab）。 */
function computeOriginalMessages(): OriginalMessage[] {
  const messages = getActiveMessages();
  const out: OriginalMessage[] = [];
  const sid = state.activeSessionId ?? '—';
  for (const m of messages) {
    if (m.role === 'assistant') {
      if (m.content) {
        out.push({
          id: `msg_${m.id}_text`,
          sessionId: sid,
          role: 'assistant',
          type: 'text',
          time: formatTimestamp(m.timestamp),
          text: m.content,
        });
      }
      if (m.thinking) {
        out.push({
          id: `msg_${m.id}_think`,
          sessionId: sid,
          role: 'assistant',
          type: 'text',
          time: formatTimestamp(m.timestamp),
          text: `💭 ${m.thinking}`,
        });
      }
      if (m.toolCalls?.length) {
        for (const tc of m.toolCalls) {
          out.push({
            id: `msg_${m.id}_tool_${tc.toolCallId}`,
            sessionId: sid,
            role: 'tool',
            type: 'function_call',
            time: formatTimestamp(m.timestamp),
            text: `${tc.name}(${tc.args ?? ''})`,
          });
          if (tc.result) {
            out.push({
              id: `msg_${m.id}_tool_${tc.toolCallId}_resp`,
              sessionId: sid,
              role: 'tool',
              type: 'function_response',
              time: formatTimestamp(m.timestamp),
              text: tc.result,
            });
          }
        }
      }
    } else if (m.role === 'user') {
      out.push({
        id: `msg_${m.id}_user`,
        sessionId: sid,
        role: 'user',
        type: 'text',
        time: formatTimestamp(m.timestamp),
        text: m.content,
      });
    } else if (m.role === 'status' || m.role === 'error') {
      out.push({
        id: `msg_${m.id}_${m.role}`,
        sessionId: sid,
        role: 'tool',
        type: 'function_response',
        time: formatTimestamp(m.timestamp),
        text: `[${m.role}] ${m.content}`,
      });
    }
  }
  return out;
}

function isFileWriteTool(name: string): boolean {
  if (name === 'file_write') return true;
  return /write|edit|patch|create/i.test(name);
}

function parseToolPath(args: string | undefined): string | null {
  if (!args) return null;
  try {
    const obj = JSON.parse(args) as { path?: unknown };
    const p = obj?.path;
    return typeof p === 'string' && p.trim() ? p : null;
  } catch {
    return null;
  }
}

function resetFileDiffCache(sessionId: string | null): void {
  if (fileDiffCacheSessionId === sessionId) return;
  fileDiffCacheSessionId = sessionId;
  fileDiffCache.clear();
  fileDiffHydrateInflight.clear();
  fileContentCache.clear();
  fileBinaryCache.clear();
  fileContentHydrateInflight.clear();
  fileContentErrors.clear();
  fileViewMode.clear();
  fileEditMode.clear();
  fileSaveInflight.clear();
  fileMissingOnDisk.clear();
  expandedFiles.clear();
  diffExpandsByPath.clear();
}

function extractFileChangesFromMessages(): FileChange[] {
  const messages = getActiveMessages();
  const byPath = new Map<string, FileChange>();
  for (const m of messages) {
    for (const tc of m.toolCalls ?? []) {
      if (!isFileWriteTool(tc.name)) continue;
      const path = parseToolPath(tc.args);
      if (!path || isPlanDocumentPath(path)) continue;
      byPath.set(path, {
        path,
        name: path.split(/[\\/]/).pop() || path,
        added: 0,
        removed: 0,
        status: 'modified',
        diff: [],
      });
    }
  }
  // Persisted turn summaries cover files created indirectly by terminal tools.
  for (const message of messages) {
    for (const file of message.turnFileChanges ?? []) {
      if (!file.path || isPlanDocumentPath(file.path)) continue;
      const status = file.status === 'added' || file.status === 'deleted' || file.status === 'modified'
        ? file.status
        : 'modified';
      byPath.set(file.path, {
        path: file.path,
        name: file.name || file.path.split(/[\\/]/).pop() || file.path,
        added: file.added || 0,
        removed: file.removed || 0,
        status,
        diff: [],
      });
    }
  }
  return Array.from(byPath.values());
}

function applyCachedDiff(files: FileChange[]): FileChange[] {
  return files.map((f) => {
    // 后端 file_changes 实时 diff 优先；磁盘缓存仅在后端无 diff 时回退
    if (f.diff.length > 0) return f;
    const cached = fileDiffCache.get(f.path);
    if (!cached?.length) return f;
    const counts = countDiffRows(cached);
    return {
      ...f,
      diff: cached,
      added: counts.added || f.added,
      removed: counts.removed || f.removed,
      status: f.status === 'modified' && counts.removed === 0 && counts.added > 0 ? 'added' : f.status,
    };
  });
}

/**
 * 合并后端累计改动与工具路径兜底。
 * 工具路径只补「后端尚未广播」的项；若后端已对账剔除（新建又删），
 * 不得再从历史 file_write 把幽灵路径加回看板。
 */
function mergeFileChangeMaps(
  fromTools: FileChange[],
  fromBackend: FileChange[],
): FileChange[] {
  const byPath = new Map<string, FileChange>();
  for (const f of fromBackend) byPath.set(f.path, f);
  if (fromBackend.length === 0) {
    for (const f of fromTools) byPath.set(f.path, f);
  } else {
    for (const f of fromTools) {
      if (byPath.has(f.path)) continue;
      // 后端已有累计列表却缺此 path → 视为对账剔除的临时文件，不回填。
    }
  }
  return Array.from(byPath.values());
}

/** Files tab：优先后端 file_changes；无后端帧时才用工具路径兜底。排除计划文档与已确认缺失路径。 */
function computeFileChanges(): FileChange[] {
  const sid = state.activeSessionId;
  resetFileDiffCache(sid);
  const fromBackend = sid ? getBookFileChanges(sid) : [];
  const fromTools = extractFileChangesFromMessages();
  if (fromBackend.length === 0 && fromTools.length === 0) return [];
  const merged = filterOutPlanDocuments(applyCachedDiff(mergeFileChangeMaps(fromTools, fromBackend)));
  // 重启后无后端累计列表时，工具路径兜底可能带回已删临时文件；读盘确认缺失后剔除。
  return merged.filter((f) => f.status === 'deleted' || !fileMissingOnDisk.has(f.path));
}

function fileChangeFromPath(path: string): FileChange {
  const name = path.split(/[\\/]/).pop() || path;
  return { path, name, added: 0, removed: 0, status: 'modified', diff: [] };
}

/** 文件消息卡打开看板时，优先展示该条消息涉及的文件；无范围时展示整个会话。 */
function currentFileChanges(): FileChange[] {
  const files = computeFileChanges();
  const prioritize = (items: FileChange[]): FileChange[] => {
    const resultExtensions = /\.(?:pptx?|docx?|xlsx?|pdf|zip)$/i;
    return [...items].sort((a, b) => Number(resultExtensions.test(b.path)) - Number(resultExtensions.test(a.path)));
  };
  if (scopedFileChanges?.length) {
    const byPath = new Map(files.map((file) => [file.path, file]));
    return prioritize(scopedFileChanges.map((file) => {
      const full = byPath.get(file.path);
      return full ? { ...file, ...full, name: file.name || full.name } : {
        ...fileChangeFromPath(file.path),
        ...file,
        name: file.name || fileChangeFromPath(file.path).name,
      };
    }));
  }
  if (!scopedFilePaths?.length) return prioritize(files);
  const byPath = new Map(files.map((file) => [file.path, file]));
  return prioritize(scopedFilePaths.map((path) => byPath.get(path) ?? fileChangeFromPath(path)));
}

async function hydrateFileDiffIfNeeded(filePath: string): Promise<void> {
  if (!filePath || fileDiffCache.has(filePath) || fileDiffHydrateInflight.has(filePath)) return;
  if (fileMissingOnDisk.has(filePath)) return;
  const sid = state.activeSessionId;
  const backendFile = sid ? getBookFileChanges(sid).find((f) => f.path === filePath) : undefined;
  if (backendFile?.status === 'deleted') return;
  if (backendFile?.diff?.length) return;
  if (!window.Crew?.readTextFile) return;
  fileDiffHydrateInflight.add(filePath);
  let listChanged = false;
  try {
    // 先静默探测，避免对已删临时文件调用 readTextFile 刷主进程 ENOENT
    if (window.Crew?.pathExists && !(await window.Crew.pathExists(filePath))) {
      fileMissingOnDisk.add(filePath);
      fileDiffCache.set(filePath, []);
      expandedFiles.delete(filePath);
      listChanged = true;
    } else {
      const text = await window.Crew.readTextFile(filePath);
      if (typeof text !== 'string' || !text) {
        fileMissingOnDisk.add(filePath);
        fileDiffCache.set(filePath, []);
        listChanged = true;
      } else {
        const rows = buildDiffFromTexts(null, text);
        if (rows.length > 0) fileDiffCache.set(filePath, rows);
      }
    }
  } catch {
    // 路径不存在 / 无权限：记缺失，避免展开时反复读盘，并从看板列表剔除
    fileMissingOnDisk.add(filePath);
    fileDiffCache.set(filePath, []);
    expandedFiles.delete(filePath);
    listChanged = true;
  } finally {
    fileDiffHydrateInflight.delete(filePath);
  }
  if (state.activeSessionId !== fileDiffCacheSessionId) return;
  // 列表变短时必须同步 tab 角标，否则会出现「内容 1 个文件、角标仍 10」
  if (listChanged) renderTabs();
  if (activeTab === 'files') renderBody();
}

/** Build an offline HTML preview document without inheriting host privileges. */
export function buildHtmlPreviewDocument(filePath: string, source: string): string {
  return buildOfflinePreviewDocument(filePath, source);
}

async function hydrateFileContentIfNeeded(filePath: string): Promise<void> {
  const kind = filePreviewKind(filePath);
  if (
    kind === 'code'
    || kind === 'legacy-office'
    || fileContentHydrateInflight.has(filePath)
    || fileContentCache.has(filePath)
    || fileBinaryCache.has(filePath)
    || fileMissingOnDisk.has(filePath)
  ) {
    return;
  }
  fileContentHydrateInflight.add(filePath);
  fileContentErrors.delete(filePath);
  try {
    if (window.Crew?.pathExists && !(await window.Crew.pathExists(filePath))) {
      fileContentErrors.set(filePath, '文件不存在，无法生成页面预览');
    } else if (isTextPreviewKind(kind) && window.Crew?.readTextFile) {
      const text = await window.Crew.readTextFile(filePath);
      if (typeof text === 'string') fileContentCache.set(filePath, text);
      else fileContentErrors.set(filePath, '文件内容无法读取');
    } else if (isBinaryPreviewKind(kind) && window.Crew?.readFileBase64) {
      const payload = await window.Crew.readFileBase64(filePath);
      if (payload?.base64) fileBinaryCache.set(filePath, payload);
      else fileContentErrors.set(filePath, '文件内容无法读取');
    } else {
      fileContentErrors.set(filePath, '当前环境不支持读取此文件');
    }
  } catch (error) {
    fileContentErrors.set(
      filePath,
      `页面预览加载失败：${error instanceof Error ? error.message : String(error)}`,
    );
  } finally {
    fileContentHydrateInflight.delete(filePath);
  }
  if (state.activeSessionId === fileDiffCacheSessionId && activeTab === 'files') renderBody();
}

/** Plan tab：读后端 todo 快照（与真实状态同步）。无 todo 工具调用时返回空。 */
function computePlanSteps(): PlanStep[] {
  const sid = state.activeSessionId;
  const todos = sid ? getBookTodos(sid) : [];
  return todosToPlanSteps(todos);
}

function todosToPlanSteps(todos: TodoItem[]): PlanStep[] {
  return todos.slice(0, 30).map((t): PlanStep => {
    const step: PlanStep = {
      id: t.id,
      name: t.content,
      status:
        t.status === 'completed' ? 'done'
        : t.status === 'in_progress' ? 'running'
        : 'waiting',
    };
    if (t.status === 'cancelled') step.meta = '已取消';
    return step;
  });
}

/** Plan 模式批准后的方案正文（book.pendingPlan）；与 todo 进度并列展示。 */
function getPendingPlanDoc(): { plan: string; planFile: string; status: PlanReviewStatus } | null {
  const sid = state.activeSessionId;
  if (!sid) return null;
  const book = sessionStore.get().books[sid];
  const pending = book?.pendingPlan;
  if (!pending || !pending.plan.trim()) return null;
  let status = pending.status;
  // 兜底：任务已全部落地、且当前不在 Plan 激活态时，不应再显示「等待审批」。
  // 常见于历史会话：批准后误再 enter / API 曾把 active+正文映射成 pending。
  if (
    isPlanActionable(status)
    && !book?.planActive
    && (book?.todos?.length ?? 0) > 0
    && book!.todos.every((t) => t.status === 'completed')
  ) {
    status = 'approved';
  }
  return {
    plan: pending.plan,
    planFile: pending.planFile || '',
    status,
  };
}

/** Plan 看板本地 UI 态：跨 refreshInspector 保留编辑草稿，避免 todo 刷新冲掉输入。 */
const planBoardUi = createPlanBoardUiState();

export type PlanBoardActions = {
  /** 批准：传入当前看板正文（含手改）。 */
  onApprove: (plan: string) => void | Promise<void>;
  /** 撤销并退出 Plan 模式。 */
  onRejectAndExit: () => void | Promise<void>;
  /** 其他：先落盘手改，再把反馈当用户消息发出。 */
  onFeedback: (plan: string, feedback: string) => void | Promise<void>;
};

let planBoardActions: PlanBoardActions | null = null;

/** 由 index / chat-controller 注入看板审批动作，避免 inspector ↔ chat 循环依赖。 */
export function setPlanBoardActions(actions: PlanBoardActions): void {
  planBoardActions = actions;
}

/** 服务端推送新计划正文时清空本地草稿，避免手改残留盖住模型修订。 */
export function resetPlanBoardDraft(nextPlan?: string): void {
  planBoardUi.draft = typeof nextPlan === 'string' ? nextPlan : null;
  planBoardUi.mode = 'preview';
  planBoardUi.otherOpen = false;
  planBoardUi.otherText = '';
}

function syncPlanBoardSession(sessionId: string | null): void {
  syncPlanBoardUiSession(planBoardUi, sessionId);
}

/** 重渲前从 DOM 捞回草稿，防止 todo_updated 刷新冲掉用户输入。 */
function capturePlanDraftFromDom(): void {
  if (activeTab !== 'plan') return;
  const editor = document.querySelector<HTMLTextAreaElement>('[data-plan-editor]');
  if (editor) {
    planBoardUi.draft = editor.value;
    planBoardUi.sessionId = state.activeSessionId;
  }
  const other = document.querySelector<HTMLTextAreaElement>('[data-plan-other-input]');
  if (other) planBoardUi.otherText = other.value;
}

function currentPlanDraft(fallback: string): string {
  return planBoardUi.draft ?? fallback;
}

let activeTab: TabKey = 'context';
const openCoreTabs = new Set<TabKey>(['context']);
const openFileTabs = new Set<string>();
let activeFileTabPath: string | null = null;
let workspaceMenuMode: 'new' | 'open' = 'new';
/** Files tab 已展开的文件路径集合（可多开，互不互斥）。 */
const expandedFiles = new Set<string>();
let scopedFilePaths: string[] | null = null;
let scopedFileChanges: FileChange[] | null = null;
/** 各文件 diff 折叠区已揭开行数（path → regionStart → {top,bottom}）。 */
const diffExpandsByPath = new Map<string, DiffRegionExpandMap>();
let expandedMsg: string | null = null;
let inspectorOpen = false; // 默认收起；首屏不由代码自动打开
const inspectorSessionUi = new InspectorSessionUiStore();
let inspectorUiSessionId: string | null = null;

function syncInspectorSessionUi(): void {
  const nextSessionId = state.activeSessionId;
  if (nextSessionId === inspectorUiSessionId) return;
  inspectorSessionUi.save(inspectorUiSessionId, {
    tab: activeTab,
    expandedFiles: [...expandedFiles],
    expandedMessage: expandedMsg,
  });
  const restored = inspectorSessionUi.load(nextSessionId);
  activeTab = restored.tab;
  activeFileTabPath = null;
  openFileTabs.clear();
  expandedFiles.clear();
  for (const path of restored.expandedFiles) expandedFiles.add(path);
  expandedMsg = restored.expandedMessage;
  diffExpandsByPath.clear();
  inspectorUiSessionId = nextSessionId;
}

function isFileExpanded(path: string): boolean {
  return expandedFiles.has(path);
}

function toggleFileExpanded(path: string): boolean {
  if (expandedFiles.has(path)) {
    expandedFiles.delete(path);
    // 折叠文件卡 → 丢弃临时揭开行数，下次展开重新全折
    clearDiffExpandsForPath(diffExpandsByPath, path);
    return false;
  }
  expandedFiles.add(path);
  return true;
}

function ensureFileExpanded(path: string): void {
  if (path) expandedFiles.add(path);
}

function fileWorkspaceTabId(path: string): string {
  return `file:${encodeURIComponent(path)}`;
}

function filePathFromWorkspaceTabId(id: string): string {
  if (!id.startsWith('file:')) return '';
  try {
    return decodeURIComponent(id.slice('file:'.length));
  } catch {
    return '';
  }
}

function fileWorkspaceTabLabel(path: string): string {
  return splitFilePathDisplay(path).name || path;
}

function fileForWorkspaceTab(path: string): FileChange {
  return currentFileChanges().find((file) => file.path === path) ?? fileChangeFromPath(path);
}

function renderFileTabView(path: string): string {
  const file = fileForWorkspaceTab(path);
  return `<div class="inspector-file-tab-view" data-file-tab-path="${escapeHtml(path)}"><div class="inspector-file-tab-view__title" title="${escapeHtml(file.path)}">${escapeHtml(file.name || file.path)}</div>${renderFileViewer(file)}</div>`;
}

function openFileWorkspaceTab(path: string): void {
  if (!path) return;
  openFileTabs.add(path);
  activeFileTabPath = path;
  activeTab = 'files';
  openCoreTabs.add('files');
  renderTabs();
  renderBody();
  void hydrateFileDiffIfNeeded(path);
  void hydrateFileContentIfNeeded(path);
}

let customViewOpen = false;

function getDiffExpands(path: string): DiffRegionExpandMap {
  return diffExpandsByPath.get(path) ?? {};
}

function setDiffExpands(path: string, expands: DiffRegionExpandMap): void {
  if (Object.keys(expands).length === 0) diffExpandsByPath.delete(path);
  else diffExpandsByPath.set(path, expands);
}

function filterOutPlanDocuments(files: FileChange[]): FileChange[] {
  return files.filter((f) => !isPlanDocumentPath(f.path));
}

/** 磁盘回读 / 参数回退生成的 diff 缓存（按会话内文件路径）。 */
const fileDiffCache = new Map<string, BackendDiffRow[]>();
const fileDiffHydrateInflight = new Set<string>();
type FileViewMode = 'preview' | 'code';
const fileContentCache = new Map<string, string>();
const fileBinaryCache = new Map<string, { base64: string; mimeType: string }>();
const fileContentHydrateInflight = new Set<string>();
const fileContentErrors = new Map<string, string>();
const fileViewMode = new Map<string, FileViewMode>();
const fileEditMode = new Set<string>();
const fileSaveInflight = new Set<string>();
/** 读盘确认不存在的路径（临时脚本写了又删）；看板不再展示，也不再重试读盘。 */
const fileMissingOnDisk = new Set<string>();
let fileDiffCacheSessionId: string | null = null;

function renderInspectorHeader(): string {
  const c = computeContextStats();
  const msgs = getActiveMessages();
  const userCount = msgs.filter((m) => m.role === 'user').length;
  const assistantCount = msgs.filter((m) => m.role === 'assistant').length;
  const pct = c.contextWindow > 0 ? Math.round((c.usedTokens / c.contextWindow) * 100) : 0;
  return `
    <div class="inspector-section">
      <div class="inspector-section__head">
        <h4 class="inspector-section__title">会话信息</h4>
        <span class="inspector-stat-pill">使用率 ${pct}%</span>
      </div>
      <div class="inspector-context-grid">
        <div class="inspector-context__row inspector-context__row--full">
          <span class="inspector-context__row-label">会话</span>
          <span class="inspector-context__row-value inspector-context__row-value--title">${escapeHtml(state.sessions.find((s) => s.id === state.activeSessionId)?.title ?? '未命名')}</span>
        </div>
        <div class="inspector-context__row"><span class="inspector-context__row-label">消息数</span><span class="inspector-context__row-value">${msgs.length}</span></div>
        <div class="inspector-context__row"><span class="inspector-context__row-label">用户消息</span><span class="inspector-context__row-value">${userCount}</span></div>
        <div class="inspector-context__row"><span class="inspector-context__row-label">助手消息</span><span class="inspector-context__row-value">${assistantCount}</span></div>
        <div class="inspector-context__row"><span class="inspector-context__row-label">供应商</span><span class="inspector-context__row-value">${escapeHtml(c.provider)}</span></div>
        <div class="inspector-context__row"><span class="inspector-context__row-label">模型</span><span class="inspector-context__row-value">${escapeHtml(c.model)}</span></div>
        <div class="inspector-context__row"><span class="inspector-context__row-label">上下文限制</span><span class="inspector-context__row-value">${fmtNum(c.contextWindow)}</span></div>
        <div class="inspector-context__row"><span class="inspector-context__row-label">总 token</span><span class="inspector-context__row-value">${fmtNum(c.usedTokens)}</span></div>
        <div class="inspector-context__row"><span class="inspector-context__row-label">输入 token</span><span class="inspector-context__row-value">${fmtNum(c.inputTokens)}</span></div>
        <div class="inspector-context__row"><span class="inspector-context__row-label">输出 token</span><span class="inspector-context__row-value">${fmtNum(c.outputTokens)}</span></div>
        <div class="inspector-context__row"><span class="inspector-context__row-label">缓存读/写</span><span class="inspector-context__row-value">${fmtNum(c.cacheRead)} / ${fmtNum(c.cacheWrite)}</span></div>
        <div class="inspector-context__row"><span class="inspector-context__row-label">创建时间</span><span class="inspector-context__row-value">${escapeHtml(c.startedAt)}</span></div>
        <div class="inspector-context__row"><span class="inspector-context__row-label">最后活动</span><span class="inspector-context__row-value">${escapeHtml(c.lastActiveAt)}</span></div>
        <div class="inspector-context__row inspector-context__row--full"><span class="inspector-context__row-label">会话 ID</span><span class="inspector-context__row-value">${escapeHtml(c.sessionId)}</span></div>
      </div>
    </div>
  `;
}

function renderContextUsage(): string {
  const c = computeContextStats();
  const pct = Math.min(100, (c.usedTokens / c.contextWindow) * 100);
  const fillClass = pct > 80 ? 'is-warn' : '';
  return `
    <div class="inspector-section">
      <h4 class="inspector-section__title">上下文用量</h4>
      <div class="inspector-meter">
        <div class="inspector-meter__head">
          <span>已用 / 窗口</span>
          <span><strong>${fmtNum(c.usedTokens)}</strong> / ${fmtNum(c.contextWindow)}</span>
        </div>
        <div class="inspector-bar">
          <div class="inspector-bar__fill ${fillClass}" data-inspector-width="${pct.toFixed(1)}"></div>
        </div>
        <div class="inspector-bar-legend">
          <span class="inspector-bar-legend__item"><span class="inspector-bar-legend__dot inspector-bar-legend__dot--input"></span>输入 ${fmtNum(c.inputTokens)}</span>
          <span class="inspector-bar-legend__item"><span class="inspector-bar-legend__dot inspector-bar-legend__dot--output"></span>输出 ${fmtNum(c.outputTokens)}</span>
        </div>
      </div>
      <div class="inspector-meter">
        <div class="inspector-meter__head">
          <span>缓存（读 / 写）</span>
          <span><strong>${fmtNum(c.cacheRead)}</strong> / ${fmtNum(c.cacheWrite)}</span>
        </div>
        <div class="inspector-bar">
          <div class="inspector-bar__fill inspector-bar__fill--cache" data-inspector-width="${Math.min(100, (c.cacheRead / Math.max(1, c.contextWindow)) * 100 * 2).toFixed(1)}"></div>
        </div>
      </div>
      <div class="inspector-meter">
        <div class="inspector-meter__head">
          <span>使用率</span>
          <span><strong>${pct.toFixed(0)}%</strong></span>
        </div>
      </div>
    </div>
  `;
}

function renderContextBreakdown(): string {
  const c = computeContextStats();
  const total = c.breakdown.reduce((s, b) => s + b.tokens, 0) || 1;
  const segs = c.breakdown
    .filter((b) => b.tokens > 0)
    .map((b) => ({ ...b, pct: (b.tokens / total) * 100 }));
  return `
    <div class="inspector-section">
      <h4 class="inspector-section__title">上下文拆分</h4>
      <div class="inspector-bar inspector-bar--breakdown">
        ${segs.map((b) => `<div class="inspector-bar__fill--${b.tone}" data-inspector-width="${b.pct.toFixed(1)}"></div>`).join('')}
      </div>
      <div class="inspector-breakdown">
        ${c.breakdown.map((b) => {
          const pct = total > 0 ? ((b.tokens / total) * 100).toFixed(1) : '0.0';
          return `
          <div class="inspector-breakdown__item">
            <span class="inspector-breakdown__dot inspector-breakdown__dot--${b.tone}"></span>
            <span>${escapeHtml(b.label)}</span>
            <span class="inspector-breakdown__value">${pct}%</span>
          </div>
        `;
        }).join('')}
      </div>
    </div>
  `;
}

function renderContextCost(): string {
  const c = computeContextStats();
  return `
    <div class="inspector-section">
      <h4 class="inspector-section__title">系统提示</h4>
      <div class="inspector-system-prompt">${escapeHtml(c.systemPrompt)}</div>
    </div>
  `;
}

function fmtNum(n: number): string {
  if (n >= 10000) return `${(n / 1000).toFixed(1)}k`;
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return n.toLocaleString();
}

function renderOriginalMessages(): string {
  const list = computeOriginalMessages();
  return `
    <div class="inspector-section">
      <div class="inspector-section__head">
        <h4 class="inspector-section__title">原始消息</h4>
        <span class="inspector-section__hint">${list.length} 条</span>
      </div>
      <div class="inspector-messages">
        ${list.length === 0 ? '<div class="inspector-empty">当前会话还没有消息，发起对话后会逐条记录。</div>' : list.map((m) => renderOriginalMessage(m)).join('')}
      </div>
    </div>
  `;
}

/** 给 JSON 文本上色：key / string / number / bool / null 各一色，对照 markdown 代码块风格。 */
function highlightJson(raw: string): string {
  // 先 escape，再做正则替换，避免 XSS 与错配
  const escaped = escapeHtml(raw);
  return escaped.replace(
    /(&quot;[^&\\]*(?:\\.[^&\\]*)*&quot;)(\s*:)?|\b(true|false|null)\b|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)/g,
    (_m, str: string, colon: string | undefined, bool: string | undefined, num: string | undefined) => {
      if (bool) return `<span class="tok-bool">${bool}</span>`;
      if (num) return `<span class="tok-num">${num}</span>`;
      if (colon) {
        return `<span class="tok-key">${str}</span><span class="tok-punct">${colon}</span>`;
      }
      return `<span class="tok-str">${str}</span>`;
    },
  );
}

function renderOriginalMessage(m: OriginalMessage): string {
  const expanded = expandedMsg === m.id;
  const roleCls = m.role === 'user' ? 'is-user' : m.role === 'tool' ? 'is-tool' : 'is-assistant';
  const json = JSON.stringify(buildMsgJSON(m), null, 2);
  const lines = json.split('\n');
  const lineNoText = lines.map((_, i) => String(i + 1)).join('\n');
  const highlighted = highlightJson(json);
  return `
    <article class="inspector-msg ${roleCls}${expanded ? ' is-expanded' : ''}" data-msg-id="${escapeHtml(m.id)}">
      <div class="inspector-msg__head" data-msg-toggle>
        <span class="inspector-msg__role">${m.role}</span>
        <span class="inspector-msg__id">${escapeHtml(m.id.slice(0, 20))}…</span>
        <span class="inspector-msg__type">${m.type}</span>
        <span class="inspector-msg__time">${escapeHtml(m.time)}</span>
        <span class="inspector-msg__chev">${expanded ? '▾' : '▸'}</span>
      </div>
      ${expanded ? `
        <div class="inspector-msg__json-wrapper">
          <pre class="inspector-msg__json-gutter" aria-hidden="true">${lineNoText}</pre>
          <pre class="inspector-msg__json"><code class="lang-json">${highlighted}</code></pre>
        </div>
      ` : ''}
    </article>
  `;
}

function buildMsgJSON(m: OriginalMessage): Record<string, unknown> {
  return {
    id: m.id,
    sessionID: m.sessionId,
    role: m.role,
    type: m.type,
    time: { created: Date.parse(m.time.replace(/\//g, '-')) || Date.now() },
    summary: { diffs: [] },
    agent: 'build',
    model: {
      providerID: getProviderFromConfig(),
      modelID: resolveSessionModelId(),
      variant: 'thinking',
    },
    parts: [
      {
        id: `prt_${m.id.slice(-10)}`,
        sessionID: m.sessionId,
        messageID: m.id,
        type: m.type,
        text: m.text,
      },
    ],
  };
}

function renderContext(): string {
  return `
    ${renderInspectorHeader()}
    ${renderContextUsage()}
    ${renderContextBreakdown()}
    ${renderContextCost()}
    ${renderOriginalMessages()}
  `;
}

function sumAdd(files: FileChange[]): number { return files.reduce((s, f) => s + f.added, 0); }
function sumDel(files: FileChange[]): number { return files.reduce((s, f) => s + f.removed, 0); }

function formatInspectorDiffCount(n: number): string {
  return Math.abs(n).toLocaleString('en-US');
}

function renderFiles(): string {
  const files = currentFileChanges();
  // 重启后无后端累计列表时，工具路径兜底可能含已删临时文件：主动探测并剔除。
  const sid = state.activeSessionId;
  const fromBackend = sid ? getBookFileChanges(sid) : [];
  if (fromBackend.length === 0) {
    for (const f of files) {
      if (f.status !== 'deleted' && !fileDiffCache.has(f.path) && !fileMissingOnDisk.has(f.path)) {
        void hydrateFileDiffIfNeeded(f.path);
      }
    }
  }
  const scoped = Boolean(scopedFilePaths?.length || scopedFileChanges?.length);
  const title = scoped ? '本条消息改动' : '本次会话改动';
  if (files.length === 0) {
    const empty = scoped
      ? '这条消息关联的文件已不在当前改动列表中。'
      : '会话内没有文件改动记录。可在配置中心打开「文件跟踪」或在消息中使用 <code>write_file</code> / <code>edit_file</code> 等工具触发跟踪。';
    return `<div class="inspector-files__head"><div class="inspector-files__titles"><h4 class="inspector-section__title">${title}</h4><span class="inspector-section__hint">0 个文件</span></div></div><div class="inspector-empty">${empty}</div>`;
  }
  const add = sumAdd(files);
  const del = sumDel(files);
  const statsBits = [
    add > 0 ? `<span class="inspector-files__stat inspector-files__stat--add">+${formatInspectorDiffCount(add)}</span>` : '',
    del > 0 ? `<span class="inspector-files__stat inspector-files__stat--del">-${formatInspectorDiffCount(del)}</span>` : '',
  ].filter(Boolean).join('');
  return `
    <div class="inspector-files__head">
      <div class="inspector-files__titles">
        <h4 class="inspector-section__title">${title}</h4>
        <div class="inspector-files__meta">
          <span class="inspector-section__hint">${files.length} 个文件</span>
          ${statsBits ? `<span class="inspector-files__stats">${statsBits}</span>` : ''}
        </div>
      </div>
    </div>
    <div class="inspector-files__list">
      ${files.map((f) => renderFile(f)).join('')}
    </div>
  `;
}

function splitFilePathDisplay(path: string): { dir: string; name: string } {
  const index = Math.max(path.lastIndexOf('\\'), path.lastIndexOf('/'));
  if (index < 0) return { dir: '', name: path };
  return { dir: path.slice(0, index + 1), name: path.slice(index + 1) };
}

const FILE_REVEAL_ICON = `
  <svg class="inspector-file__reveal-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden="true">
    <path d="M14 3h7v7" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
    <path d="M10 14 21 3" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
    <path d="M21 14v6a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
  </svg>
`;

const FILE_CHEV_ICON = `
  <svg class="inspector-file__chev-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden="true">
    <path d="M9 6l6 6-6 6" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/>
  </svg>
`;

const FILE_EDIT_ICON = `
  <svg viewBox="0 0 24 24" aria-hidden="true">
    <path d="M12 20h9"/>
    <path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/>
  </svg>
`;

const FILE_VIEW_ICON = `
  <svg viewBox="0 0 24 24" aria-hidden="true">
    <path d="M2 12s3.5-6 10-6 10 6 10 6-3.5 6-10 6S2 12 2 12Z"/>
    <circle cx="12" cy="12" r="3"/>
  </svg>
`;

function previewLabel(kind: FilePreviewKind): string {
  const labels: Record<FilePreviewKind, string> = {
    html: '页面预览',
    svg: '矢量图预览',
    markdown: 'Markdown 预览',
    pdf: 'PDF 预览',
    image: '图片预览',
    docx: 'Word 预览',
    pptx: 'PPT 预览',
    xlsx: 'Excel 预览',
    'legacy-office': '办公文件预览',
    code: '代码改动',
  };
  return labels[kind];
}

function isBoardEditableKind(kind: FilePreviewKind): boolean {
  return kind === 'markdown' || kind === 'docx' || kind === 'pptx' || kind === 'xlsx';
}

function renderPendingFileLoadState(filePath: string, label: string): string {
  return `<div class="inspector-file__preview-state" data-file-load-pending="${escapeHtml(filePath)}">${escapeHtml(label)}</div>`;
}

function renderFileEditContent(file: FileChange, kind: FilePreviewKind): string {
  const source = fileContentCache.get(file.path);
  const binary = fileBinaryCache.get(file.path);
  const error = fileContentErrors.get(file.path);
  if (error) return `<div class="inspector-file__preview-state">${escapeHtml(error)}</div>`;
  if (kind === 'markdown') {
    return source == null
      ? renderPendingFileLoadState(file.path, '正在加载 Markdown…')
      : `<textarea class="inspector-file-editor inspector-file-editor--markdown" data-file-editor="${escapeHtml(file.path)}" spellcheck="false">${escapeHtml(source)}</textarea>`;
  }
  if ((kind === 'docx' || kind === 'pptx') && binary) {
    const label = kind === 'docx' ? 'Word 页面' : 'PPT 幻灯片';
    return `<div class="inspector-office-preview inspector-office-preview--${kind} inspector-office-page-editor-host" data-office-page-editor="${escapeHtml(file.path)}" data-office-kind="${kind}"><div class="inspector-file__preview-state">正在打开可编辑${label}…</div></div>`;
  }
  if (kind === 'xlsx' && binary) {
    return `<div class="inspector-office-editor inspector-office-editor--xlsx" data-office-editor="${escapeHtml(file.path)}" data-office-kind="xlsx"><div class="inspector-file__preview-state">正在加载工作表编辑器…</div></div>`;
  }
  return renderPendingFileLoadState(file.path, `正在加载${previewLabel(kind)}…`);
}

function renderFilePreviewContent(file: FileChange, kind: FilePreviewKind): string {
  const source = fileContentCache.get(file.path);
  const binary = fileBinaryCache.get(file.path);
  const error = fileContentErrors.get(file.path);
  if (error) return `<div class="inspector-file__preview-state">${escapeHtml(error)}</div>`;
  if (kind === 'legacy-office') {
    return '<div class="inspector-file__preview-state">旧版 .doc / .ppt 暂不支持离线预览，请另存为 .docx / .pptx。</div>';
  }
  if (kind === 'html' && source != null) {
    return `<iframe class="inspector-file__preview-frame" title="${escapeHtml(file.name)} 页面预览" sandbox="allow-scripts allow-modals" srcdoc="${escapeHtml(buildHtmlPreviewDocument(file.path, source))}"></iframe>`;
  }
  if (kind === 'svg' && source != null) {
    return `<div class="inspector-file__svg-preview"><img alt="${escapeHtml(file.name)}" data-file-svg-preview="${escapeHtml(file.path)}"></div>`;
  }
  if (kind === 'markdown' && source != null) {
    return `<div class="inspector-file__markdown-preview chat-markdown">${renderMarkdownHtml(source)}</div>`;
  }
  if ((kind === 'pdf' || kind === 'image') && binary) {
    return kind === 'pdf'
      ? `<iframe class="inspector-file__preview-frame inspector-file__preview-frame--pdf" title="${escapeHtml(file.name)} PDF 预览" data-file-binary-preview="${escapeHtml(file.path)}"></iframe>`
      : `<div class="inspector-file__image-preview"><img alt="${escapeHtml(file.name)}" data-file-binary-preview="${escapeHtml(file.path)}"></div>`;
  }
  if ((kind === 'docx' || kind === 'pptx' || kind === 'xlsx') && binary) {
    const label = kind === 'docx' ? '文档' : kind === 'pptx' ? '幻灯片' : '工作簿';
    return `<div class="inspector-office-preview inspector-office-preview--${kind}" data-office-preview="${escapeHtml(file.path)}" data-office-kind="${kind}"><div class="inspector-file__preview-state">正在渲染${label}…</div></div>`;
  }
  return renderPendingFileLoadState(file.path, `正在加载${previewLabel(kind)}…`);
}

function renderFileViewer(file: FileChange): string {
  const kind = filePreviewKind(file.path);
  const previewable = file.status !== 'deleted' && kind !== 'code';
  const hasCodeView = previewable && isTextPreviewKind(kind);
  const editable = file.status !== 'deleted' && isBoardEditableKind(kind);
  const editing = editable && fileEditMode.has(file.path);
  const saving = fileSaveInflight.has(file.path);
  const viewMode: FileViewMode = hasCodeView
    ? (fileViewMode.get(file.path) ?? 'preview')
    : previewable ? 'preview' : 'code';
  const codeHtml = file.diff.length > 0
    ? renderDiffPanelHtml(file.diff, {
      escapeHtml,
      filename: file.name,
      expands: getDiffExpands(file.path),
    })
    : fileDiffHydrateInflight.has(file.path)
      ? '<div class="inspector-file__diff-empty">正在读取文件 diff…</div>'
      : file.status === 'deleted'
        ? '<div class="inspector-file__diff-empty">文件已删除（本轮曾修改后移除）</div>'
        : '<div class="inspector-file__diff-empty">暂无 diff 内容（可尝试重新展开或确认文件仍在本地）</div>';
  if (!previewable) return codeHtml;
  const editControls = editable
    ? `${editing
      ? `<button type="button" class="inspector-file__icon-toggle" data-file-edit-toggle="${escapeHtml(file.path)}" aria-label="切换到查看模式" title="查看">${FILE_VIEW_ICON}</button>`
      : `<button type="button" class="inspector-file__icon-toggle" data-file-edit-toggle="${escapeHtml(file.path)}" aria-label="切换到编辑模式" title="编辑">${FILE_EDIT_ICON}</button>`}
      ${editing ? `<button type="button" class="inspector-file__save" data-file-save="${escapeHtml(file.path)}" ${saving ? 'disabled' : ''}>${saving ? '保存中…' : '保存'}</button>` : ''}`
    : '';
  return `<div class="inspector-file__viewer">
        <div class="inspector-file__viewer-toolbar">
          <span class="inspector-file__viewer-label">${editing ? '编辑' : viewMode === 'preview' ? previewLabel(kind) : '代码改动'}</span>
          <div class="inspector-file__viewer-actions">
            ${hasCodeView && !editing ? `<button type="button" class="inspector-file__view-toggle" data-file-view-toggle="${escapeHtml(file.path)}" aria-pressed="${viewMode === 'code'}">${viewMode === 'preview' ? '查看代码' : '查看预览'}</button>` : ''}
            ${editControls}
          </div>
        </div>
        ${editing
          ? renderFileEditContent(file, kind)
          : viewMode === 'preview'
            ? renderFilePreviewContent(file, kind)
            : codeHtml}
      </div>`;
}

function renderFile(f: FileChange): string {
  const isActive = isFileExpanded(f.path);
  const iconChar = f.status === 'added' ? '+' : f.status === 'deleted' ? '−' : 'M';
  const iconTone = f.status === 'added' ? 'added' : f.status === 'deleted' ? 'deleted' : 'modified';
  const statusLabel = f.status === 'added' ? '新增' : f.status === 'deleted' ? '删除' : '修改';
  const { dir, name } = splitFilePathDisplay(f.path);
  const viewerHtml = renderFileViewer(f);
  const addHtml = f.added > 0 ? `<span class="inspector-file__add">+${formatInspectorDiffCount(f.added)}</span>` : '';
  const delHtml = f.removed > 0 ? `<span class="inspector-file__del">-${formatInspectorDiffCount(f.removed)}</span>` : '';
  const revealDisabled = f.status === 'deleted';
  const revealBtn = revealDisabled
    ? `<button type="button" class="inspector-file__reveal is-disabled" disabled title="文件已删除" aria-label="文件已删除">${FILE_REVEAL_ICON}</button>`
    : `<button type="button" class="inspector-file__reveal" data-file-reveal="${escapeHtml(f.path)}" title="打开方式" aria-label="${escapeHtml(name)} 的打开方式" aria-haspopup="menu" aria-expanded="false">${FILE_REVEAL_ICON}</button>`;
  return `
    <article class="inspector-file inspector-file--${iconTone}${isActive ? ' is-active' : ''}" data-file-path="${escapeHtml(f.path)}" data-file-status="${escapeHtml(f.status)}">
      <div class="inspector-file__head-row">
        <button type="button" class="inspector-file__head" data-file-toggle aria-expanded="${isActive}">
          <span class="inspector-file__icon inspector-file__icon--${iconTone}" aria-hidden="true">${iconChar}</span>
          <span class="inspector-file__copy">
            <span class="inspector-file__pathline" title="${escapeHtml(f.path)}">${dir ? `<span class="inspector-file__path-dir">${escapeHtml(dir)}</span>` : ''}<span class="inspector-file__path-name">${escapeHtml(name)}</span></span>
            <span class="inspector-file__status-badge inspector-file__status-badge--${iconTone}">${statusLabel}</span>
          </span>
          <span class="inspector-file__tail">
            <span class="inspector-file__stats">${addHtml}${delHtml}</span>
            <span class="inspector-file__chev" aria-hidden="true">${FILE_CHEV_ICON}</span>
          </span>
        </button>
        ${revealBtn}
      </div>
      ${isActive ? `<div class="inspector-file__diff">${viewerHtml}</div>` : ''}
    </article>
  `;
}

function mountFilePreviews(root: HTMLElement): void {
  root.querySelectorAll<HTMLElement>('[data-file-load-pending]').forEach((element) => {
    const filePath = element.getAttribute('data-file-load-pending');
    if (filePath) void hydrateFileContentIfNeeded(filePath);
  });
  root.querySelectorAll<HTMLImageElement>('[data-file-svg-preview]').forEach((image) => {
    const filePath = image.getAttribute('data-file-svg-preview');
    const source = filePath ? fileContentCache.get(filePath) : undefined;
    if (!source) return;
    const sanitized = DOMPurify.sanitize(source, {
      USE_PROFILES: { svg: true, svgFilters: true },
      FORBID_TAGS: ['script', 'foreignObject'],
      FORBID_ATTR: ['onload', 'onclick', 'onerror'],
    });
    image.src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(sanitized)}`;
  });
  root.querySelectorAll<HTMLElement>('[data-file-binary-preview]').forEach((element) => {
    const filePath = element.getAttribute('data-file-binary-preview');
    const payload = filePath ? fileBinaryCache.get(filePath) : undefined;
    if (!payload) return;
    const dataUrl = `data:${payload.mimeType};base64,${payload.base64}`;
    if (element instanceof HTMLIFrameElement || element instanceof HTMLImageElement) {
      element.src = dataUrl;
    }
  });
  root.querySelectorAll<HTMLElement>('[data-office-preview]').forEach((container) => {
    if (container.dataset.previewMounted === 'true') return;
    const filePath = container.getAttribute('data-office-preview');
    const kind = container.getAttribute('data-office-kind');
    const payload = filePath ? fileBinaryCache.get(filePath) : undefined;
    if (!payload || (kind !== 'docx' && kind !== 'pptx' && kind !== 'xlsx')) return;
    container.dataset.previewMounted = 'true';
    void import('../office-preview')
      .then(async ({ renderDocxPreview, renderPptxPreview, renderXlsxPreview }) => {
        if (!container.isConnected) return;
        if (kind === 'docx') await renderDocxPreview(payload.base64, container);
        else if (kind === 'pptx') await renderPptxPreview(payload.base64, container);
        else await renderXlsxPreview(payload.base64, container);
      })
      .catch((error: unknown) => {
        if (!container.isConnected) return;
        container.innerHTML = `<div class="inspector-file__preview-state">${escapeHtml(`预览渲染失败：${error instanceof Error ? error.message : String(error)}`)}</div>`;
      });
  });
  root.querySelectorAll<HTMLElement>('[data-office-page-editor]').forEach((container) => {
    if (container.dataset.previewMounted === 'true') return;
    const filePath = container.getAttribute('data-office-page-editor');
    const kind = container.getAttribute('data-office-kind');
    const payload = filePath ? fileBinaryCache.get(filePath) : undefined;
    if (!payload || (kind !== 'docx' && kind !== 'pptx')) return;
    container.dataset.previewMounted = 'true';
    void import('../office-preview')
      .then(async ({ renderDocxPreview, renderPptxPreview }) => {
        if (!container.isConnected) return;
        if (kind === 'docx') {
          await renderDocxPreview(payload.base64, container, { editable: true });
        } else {
          await renderPptxPreview(payload.base64, container, { editable: true });
        }
      })
      .catch((error: unknown) => {
        if (!container.isConnected) return;
        container.innerHTML = `<div class="inspector-file__preview-state">${escapeHtml(`编辑页面打开失败：${error instanceof Error ? error.message : String(error)}`)}</div>`;
      });
  });
  root.querySelectorAll<HTMLElement>('[data-office-editor]').forEach((container) => {
    if (container.dataset.editorMounted === 'true') return;
    const filePath = container.getAttribute('data-office-editor');
    const kind = container.getAttribute('data-office-kind');
    const payload = filePath ? fileBinaryCache.get(filePath) : undefined;
    if (!payload || kind !== 'xlsx') return;
    container.dataset.editorMounted = 'true';
    void import('../office-edit')
      .then(async ({ extractXlsxSheet }) => {
        const sheet = await extractXlsxSheet(payload.base64);
        if (!container.isConnected || !filePath) return;
        const { mountXlsxEditor } = await import('../xlsx-editor');
        container.innerHTML = `<div class="inspector-xlsx-editor" data-xlsx-editor="${escapeHtml(filePath)}"></div>`;
        const editor = container.querySelector<HTMLElement>('[data-xlsx-editor]');
        if (editor) mountXlsxEditor(editor, sheet);
      })
      .catch((error: unknown) => {
        if (!container.isConnected) return;
        container.innerHTML = `<div class="inspector-file__preview-state">${escapeHtml(`编辑内容加载失败：${error instanceof Error ? error.message : String(error)}`)}</div>`;
      });
  });
}

/** 在系统资源管理器中显示并选中文件（消息卡与看板 Files 共用）。 */
export async function revealPathInFolder(targetPath: string): Promise<void> {
  if (!window.Crew?.showItemInFolder) {
    notify('当前环境不支持打开文件夹');
    return;
  }
  try {
    await window.Crew.showItemInFolder(targetPath);
  } catch (err) {
    notify(`打开失败：${err instanceof Error ? err.message : String(err)}`);
  }
}

function renderPlanActions(doc: { plan: string; status: PlanReviewStatus }): string {
  if (!isPlanActionable(doc.status)) {
    return `<div class="inspector-plan-board__actions inspector-plan-board__actions--readonly"><span class="inspector-plan-board__note">历史计划只读展示</span></div>`;
  }
  const otherOpen = planBoardUi.otherOpen;
  return `
    <div class="inspector-plan-board__actions" role="group" aria-label="计划审批">
      <div class="inspector-plan-board__action-row">
        <button type="button" class="inspector-plan-board__btn" data-plan-board-action="other" aria-pressed="${otherOpen ? 'true' : 'false'}">其他</button>
        <button type="button" class="inspector-plan-board__btn" data-plan-board-action="reject_and_exit">撤销</button>
        <button type="button" class="inspector-plan-board__btn inspector-plan-board__btn--primary" data-plan-board-action="approve">批准并执行</button>
      </div>
      <div class="inspector-plan-board__other"${otherOpen ? '' : ' hidden'}>
        <label class="inspector-plan-board__other-label" for="inspector-plan-other-input">修改要求</label>
        <textarea id="inspector-plan-other-input" class="inspector-plan-board__other-input" data-plan-other-input rows="3" placeholder="说明哪里不满意，例如：去掉粒子特效，改成更简洁的 UI">${escapeHtml(planBoardUi.otherText)}</textarea>
        <button type="button" class="inspector-plan-board__btn inspector-plan-board__btn--primary" data-plan-board-action="submit_other">提交修改要求</button>
      </div>
    </div>
  `;
}

function renderPlanDocSection(doc: { plan: string; planFile: string; status: PlanReviewStatus }): string {
  const draft = currentPlanDraft(doc.plan);
  const title = firstPlanHeading(draft) || '计划方案';
  const actionable = isPlanActionable(doc.status);
  const editMode = actionable && planBoardUi.mode === 'edit';
  const modeToggle = actionable
    ? `<div class="inspector-plan-board__mode" role="tablist" aria-label="计划编辑模式">
        <button type="button" class="inspector-plan-board__mode-btn${editMode ? '' : ' is-active'}" data-plan-mode="preview" role="tab" aria-selected="${editMode ? 'false' : 'true'}">预览</button>
        <button type="button" class="inspector-plan-board__mode-btn${editMode ? ' is-active' : ''}" data-plan-mode="edit" role="tab" aria-selected="${editMode ? 'true' : 'false'}">编辑</button>
      </div>`
    : '';
  const body = editMode
    ? `<textarea class="inspector-plan-board__editor" data-plan-editor rows="14" spellcheck="false" aria-label="编辑计划正文">${escapeHtml(draft)}</textarea>`
    : `<div class="inspector-plan-board__doc-body md-body chat-markdown">${renderMarkdownHtml(draft || '(计划为空)')}</div>`;
  return `
    <section class="inspector-plan-board__doc" aria-label="计划方案">
      <div class="inspector-section__head">
        <h4 class="inspector-section__title">计划方案</h4>
        <span class="inspector-plan-board__badge inspector-plan-board__badge--${escapeHtml(doc.status)}">${escapeHtml(planStatusLabel(doc.status))}</span>
      </div>
      <div class="inspector-plan-board__doc-card">
        <div class="inspector-plan-board__doc-top">
          <div class="inspector-plan-board__doc-title">${escapeHtml(title)}</div>
          ${modeToggle}
        </div>
        ${doc.planFile ? `<div class="inspector-plan-board__doc-file" title="${escapeHtml(doc.planFile)}">${escapeHtml(doc.planFile)}</div>` : ''}
        ${body}
        ${renderPlanActions(doc)}
      </div>
    </section>
  `;
}

function renderTodoSection(steps: PlanStep[]): string {
  const done = steps.filter((s) => s.status === 'done').length;
  return `
    <section class="inspector-plan-board__todos" aria-label="任务进度">
      <div class="inspector-section__head">
        <h4 class="inspector-section__title">任务进度</h4>
        <span class="inspector-section__hint">${done}/${steps.length} 已完成</span>
      </div>
      <div class="inspector-plan">
        ${steps.map((s, i) => `
          <article class="inspector-plan__step inspector-plan__step--${s.status}">
            <div class="inspector-plan__bullet">${s.status === 'done' ? '✓' : i + 1}</div>
            <div class="inspector-plan__step-body">
              <div class="inspector-plan__step-name">${escapeHtml(s.name)}</div>
              ${s.meta ? `<div class="inspector-plan__step-meta">${escapeHtml(s.meta)}</div>` : ''}
            </div>
          </article>
        `).join('')}
      </div>
    </section>
  `;
}

/** Plan tab：同步展示 Plan 模式方案正文 + todo 进度（二者可独立存在）。 */
function renderPlan(): string {
  syncPlanBoardSession(state.activeSessionId);
  const doc = getPendingPlanDoc();
  const steps = computePlanSteps();
  if (!doc && !steps.length) {
    return `
      <div class="inspector-plan-board">
        <div class="inspector-section__head">
          <h4 class="inspector-section__title">执行计划</h4>
          <span class="inspector-section__hint">无</span>
        </div>
        <div class="inspector-plan__empty" role="status">
          当前会话还没有计划方案或任务进度。Agent 提交计划后会自动打开本页签，供你直接编辑、批准、撤销或提出修改要求。
        </div>
      </div>
    `;
  }
  const hintParts: string[] = [];
  if (doc) hintParts.push(planStatusLabel(doc.status));
  if (steps.length) {
    const done = steps.filter((s) => s.status === 'done').length;
    hintParts.push(`${done}/${steps.length} 任务`);
  }
  return `
    <div class="inspector-plan-board">
      <div class="inspector-section__head">
        <h4 class="inspector-section__title">执行计划</h4>
        <span class="inspector-section__hint">${escapeHtml(hintParts.join(' · '))}</span>
      </div>
      ${doc ? renderPlanDocSection(doc) : ''}
      ${steps.length ? renderTodoSection(steps) : ''}
    </div>
  `;
}

function firstPlanHeading(markdown: string): string | null {
  const line = markdown
    .split('\n')
    .map((part) => part.trim())
    .find((part) => part.length > 0);
  return line ? line.replace(/^#{1,6}\s+/, '').slice(0, 90) : null;
}

function renderKanban(): string {
  return buildKanbanInspectorHtml();
}

function renderCollaboration(): string {
  return buildTeamCollaborationBoardHtml();
}

/** 重渲前采集 inspector 内可滚动节点的 scrollTop，避免流式 todo/delta 刷新把用户滚回顶部。 */
function captureInspectorScroll(body: HTMLElement): Array<{ sel: string; top: number }> {
  const sels = [
    '.inspector-plan-board__doc-body',
    '.inspector-plan-board__editor',
    '.kanban-board__scrollable',
    '.team-board__list',
    '.inspector-file__diff',
    '.inspector-msg__body',
  ];
  const saved: Array<{ sel: string; top: number }> = [];
  for (const sel of sels) {
    const el = body.querySelector(sel) as HTMLElement | null;
    if (el && el.scrollTop > 0) saved.push({ sel, top: el.scrollTop });
  }
  return saved;
}

function restoreInspectorScroll(
  body: HTMLElement,
  bodyTop: number,
  nested: Array<{ sel: string; top: number }>,
): void {
  body.scrollTop = bodyTop;
  for (const { sel, top } of nested) {
    const el = body.querySelector(sel) as HTMLElement | null;
    if (el) el.scrollTop = top;
  }
}

function renderBody(): void {
  const body = $('#chat-inspector-body');
  if (!body) return;
  capturePlanDraftFromDom();
  // 流式期间 renderChat / todo_updated 会频繁 refreshInspector；innerHTML 重建会丢滚动。
  const savedBodyTop = body.scrollTop;
  const savedNested = captureInspectorScroll(body);
  if (activeTab !== 'kanban') {
    disconnectKanbanObserver();
  }
  if (activeTab !== 'collaboration') stopTeamCollaborationPolling();
  // innerHTML below destroys the stage node even when Browser stays active.
  // Detach the native remote view first so it can never cover stale UI bounds.
  hideBrowserPanelView();
  let html = '';
  if (activeTab === 'files') {
    html = activeFileTabPath ? renderFileTabView(activeFileTabPath) : renderFiles();
  }
  else if (activeTab === 'context') html = renderContext();
  else if (activeTab === 'plan') html = renderPlan();
  else if (activeTab === 'kanban') html = renderKanban();
  else if (activeTab === 'collaboration') html = renderCollaboration();
  else if (activeTab === 'browser') html = renderBrowserPanel();
  body.classList.toggle('is-team-collaboration', activeTab === 'collaboration');
  body.innerHTML = html;
  body.querySelectorAll<HTMLElement>('[data-inspector-width]').forEach((fill) => {
    setRuntimeStyle(fill, 'width', `${fill.dataset.inspectorWidth ?? '0'}%`);
  });
  if (activeTab === 'kanban') activateKanbanInspectorTab();
  if (activeTab === 'collaboration') activateTeamCollaborationBoard();
  if (activeTab === 'files') applyDiffSyntaxHighlights(body, escapeHtml);
  bindBodyEvents();
  if (activeTab === 'files') mountFilePreviews(body);
  if (activeTab === 'browser') bindBrowserPanel();
  restoreInspectorScroll(body, savedBodyTop, savedNested);
}

const TAB_LABELS: Record<TabKey, string> = {
  context: '上下文',
  files: '文件',
  plan: '计划',
  kanban: '任务',
  collaboration: '协作',
  browser: '浏览器',
};

const TAB_ICONS: Record<TabKey, string> = {
  context: '<svg viewBox="0 0 24 24" aria-hidden="true"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v6c0 1.66 4.03 3 9 3s9-1.34 9-3V5M3 11v6c0 1.66 4.03 3 9 3s9-1.34 9-3v-6"/></svg>',
  files: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg>',
  plan: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 6h18M3 12h18M3 18h12"/></svg>',
  kanban: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M8 10h8M8 14h5"/></svg>',
  collaboration: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="7" cy="7" r="3"/><circle cx="17" cy="7" r="3"/><circle cx="12" cy="17" r="3"/></svg>',
  browser: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a15 15 0 0 1 0 18M12 3a15 15 0 0 0 0 18"/></svg>',
};

const TAB_CLOSE_ICON = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m7 7 10 10M17 7 7 17"/></svg>';

function renderWorkspaceTab(id: string, label: string, icon: string, active: boolean, count?: number): string {
  return `<div class="chat-inspector__tab${active ? ' is-active' : ''}" role="presentation">
    <button type="button" class="chat-inspector__tab-select" data-workspace-tab="${escapeHtml(id)}" role="tab" aria-selected="${active}" title="${escapeHtml(label)}">
      ${icon}<span class="chat-inspector__tab-label">${escapeHtml(label)}</span>${typeof count === 'number' ? `<span class="chat-inspector__tab-count">${count}</span>` : ''}
    </button>
    <button type="button" class="chat-inspector__tab-close" data-workspace-tab-close="${escapeHtml(id)}" aria-label="关闭 ${escapeHtml(label)}" title="关闭标签页">${TAB_CLOSE_ICON}</button>
  </div>`;
}

function createInspectorTabIcon(markup: string): SVGElement {
  // TAB_ICONS is a local constant map, not renderer input. Parse it as a
  // static SVG node so menu labels and ids never cross an HTML interpolation
  // boundary.
  const parsed = new DOMParser().parseFromString(markup, 'image/svg+xml').documentElement;
  if (parsed.nodeName.toLowerCase() !== 'svg') throw new Error('invalid inspector tab icon');
  return document.importNode(parsed, true) as unknown as SVGElement;
}

function appendInspectorTabMenuItem(
  menu: HTMLElement,
  item: { id: string; label: string; icon: string },
  attribute: 'data-workspace-tab' | 'data-workspace-entry',
): void {
  const button = document.createElement('button');
  button.type = 'button';
  button.setAttribute('role', 'menuitem');
  button.setAttribute(attribute, item.id);
  button.append(createInspectorTabIcon(item.icon));
  const label = document.createElement('span');
  label.textContent = item.label;
  button.append(label);
  menu.append(button);
}

function renderInspectorTabMenu(): void {
  const menu = document.getElementById('inspector-tab-menu');
  if (!menu) return;
  const teamSession = isExternalTeamSession();
  menu.replaceChildren();
  if (workspaceMenuMode === 'open') {
    const items = [...openCoreTabs]
      .filter((tab) => tab !== 'collaboration' || teamSession)
      .map((tab) => ({ id: `core:${tab}`, label: TAB_LABELS[tab], icon: TAB_ICONS[tab] }));
    const fileItems = [...openFileTabs].map((path) => ({
      id: fileWorkspaceTabId(path),
      label: fileWorkspaceTabLabel(path),
      icon: TAB_ICONS.files,
    }));
    const heading = document.createElement('div');
    heading.className = 'chat-inspector__tab-menu-label';
    heading.textContent = '已打开';
    menu.append(heading);
    if (items.length === 0 && fileItems.length === 0) {
      const empty = document.createElement('div');
      empty.className = 'chat-inspector__tab-menu-empty';
      empty.textContent = '暂无标签页';
      menu.append(empty);
    } else {
      items.forEach((item) => appendInspectorTabMenuItem(menu, item, 'data-workspace-tab'));
      fileItems.forEach((item) => appendInspectorTabMenuItem(menu, item, 'data-workspace-tab'));
    }
    return;
  }
  const entries: Array<{ id: string; label: string; icon: string }> = [
    { id: 'context', label: TAB_LABELS.context, icon: TAB_ICONS.context },
    { id: 'files', label: TAB_LABELS.files, icon: TAB_ICONS.files },
    { id: 'plan', label: TAB_LABELS.plan, icon: TAB_ICONS.plan },
    { id: 'kanban', label: TAB_LABELS.kanban, icon: TAB_ICONS.kanban },
    ...(teamSession ? [{ id: 'collaboration', label: TAB_LABELS.collaboration, icon: TAB_ICONS.collaboration }] : []),
    { id: 'browser:new', label: TAB_LABELS.browser, icon: TAB_ICONS.browser },
  ];
  const heading = document.createElement('div');
  heading.className = 'chat-inspector__tab-menu-label';
  heading.textContent = '新增页面';
  menu.append(heading);
  entries.forEach((item) => appendInspectorTabMenuItem(menu, item, 'data-workspace-entry'));
}

function updateOpenTabMenuToggleVisibility(): void {
  const tabs = document.getElementById('chat-inspector-tabs');
  const toggle = document.getElementById('inspector-tab-picker-toggle') as HTMLButtonElement | null;
  if (!tabs || !toggle) return;
  const overflow = tabs.scrollWidth > tabs.clientWidth + 1;
  toggle.hidden = !overflow;
  toggle.classList.toggle('is-hidden', !overflow);
  if (!overflow && workspaceMenuMode === 'open') {
    const menu = document.getElementById('inspector-tab-menu');
    if (menu) menu.hidden = true;
    toggle.setAttribute('aria-expanded', 'false');
  }
}

function activateWorkspaceTab(id: string): void {
  if (id.startsWith('file:')) {
    const path = filePathFromWorkspaceTabId(id);
    if (path && openFileTabs.has(path)) openFileWorkspaceTab(path);
    return;
  }
  if (!id.startsWith('core:')) {
    if (id === 'browser:new') openBrowserWorkbench({ createTab: true });
    return;
  }
  const tab = id.slice(5) as TabKey;
  if (!TAB_LABELS[tab] || (tab === 'collaboration' && !isExternalTeamSession())) return;
  openCoreTabs.add(tab);
  if (tab === 'files') {
    scopedFilePaths = null;
    scopedFileChanges = null;
  }
  setTab(tab);
}

function closeWorkspaceTab(id: string): void {
  if (id.startsWith('file:')) {
    const path = filePathFromWorkspaceTabId(id);
    if (!path) return;
    openFileTabs.delete(path);
    if (activeFileTabPath === path) {
      activeFileTabPath = null;
      activeTab = 'files';
      renderTabs();
      renderBody();
    } else {
      renderTabs();
    }
    return;
  }
  if (!id.startsWith('core:')) return;
  const tab = id.slice(5) as TabKey;
  openCoreTabs.delete(tab);
  if (activeTab === tab) {
    const fallback = [...openCoreTabs].find((candidate) => candidate !== 'collaboration' || isExternalTeamSession());
    setTab(fallback || 'context');
    openCoreTabs.add(fallback || 'context');
  } else {
    renderTabs();
  }
}

function openWorkspaceEntry(id: string): void {
  if (id === 'browser:new') {
    openBrowserWorkbench({ createTab: true });
    return;
  }
  activateWorkspaceTab(`core:${id}`);
}

function renderTabs(): void {
  syncInspectorSessionUi();
  const teamSession = isExternalTeamSession();
  if (activeTab === 'collaboration' && !teamSession) {
    stopTeamCollaborationPolling();
    activeTab = 'context';
  }
  if (teamSession) openCoreTabs.add('collaboration');
  else openCoreTabs.delete('collaboration');
  if (activeTab !== 'browser') openCoreTabs.add(activeTab);
  const tasks = (state.kanbanBoard as { tasks?: unknown[] } | null)?.tasks;
  const counts: Partial<Record<TabKey, number>> = {
    context: computeOriginalMessages().length,
    files: currentFileChanges().length,
    kanban: tasks?.length ?? 0,
    collaboration: teamCollaborationTaskCount(),
  };
  const tabs = document.getElementById('chat-inspector-tabs');
  if (tabs) {
    const browserTab = activeTab === 'browser'
      ? renderWorkspaceTab('core:browser', '新标签页', TAB_ICONS.browser, true)
      : '';
    tabs.innerHTML = [...openCoreTabs]
      .filter((tab) => tab !== 'collaboration' || teamSession)
      .map((tab) => renderWorkspaceTab(`core:${tab}`, TAB_LABELS[tab], TAB_ICONS[tab], activeTab === tab, counts[tab]))
      .join('')
      + browserTab
      + [...openFileTabs]
        .map((path) => renderWorkspaceTab(
          fileWorkspaceTabId(path),
          fileWorkspaceTabLabel(path),
          TAB_ICONS.files,
          activeFileTabPath === path,
        ))
        .join('');
  }
  renderInspectorTabMenu();
  updateOpenTabMenuToggleVisibility();
  window.requestAnimationFrame?.(() => updateOpenTabMenuToggleVisibility());
}

function collectOfficeBlockElements(scope: ParentNode): HTMLElement[] {
  const direct = Array.from(scope.querySelectorAll<HTMLElement>('[data-office-block]'));
  const nested = Array.from(scope.querySelectorAll<HTMLElement>('*'))
    .flatMap((element) => (
      element.shadowRoot ? collectOfficeBlockElements(element.shadowRoot) : []
    ));
  return [...direct, ...nested];
}

function collectOfficeEditorBlockEdits(filePath: string): Array<{ index: number; text: string }> {
  const root = document.querySelector<HTMLElement>(
    `[data-office-page-editor="${CSS.escape(filePath)}"]`,
  );
  if (!root) return [];
  return collectOfficeBlockElements(root)
    .map((block) => ({
      index: Number(block.dataset.officeBlock ?? 0),
      text: block instanceof HTMLTextAreaElement ? block.value : (block.textContent ?? ''),
    }))
    .filter((edit) => Number.isFinite(edit.index))
    .sort((left, right) => left.index - right.index);
}

function invalidateFilePreviewCache(filePath: string): void {
  fileContentCache.delete(filePath);
  fileBinaryCache.delete(filePath);
  fileContentErrors.delete(filePath);
  fileDiffCache.delete(filePath);
  fileMissingOnDisk.delete(filePath);
}

async function saveFileEditor(filePath: string): Promise<void> {
  if (fileSaveInflight.has(filePath)) return;
  const kind = filePreviewKind(filePath);
  try {
    if (kind === 'markdown') {
      const editor = document.querySelector<HTMLTextAreaElement>(
        `[data-file-editor="${CSS.escape(filePath)}"]`,
      );
      if (!editor) throw new Error('未找到 Markdown 编辑器');
      if (!window.Crew?.writeTextFile) throw new Error('当前环境不支持保存文本文件');
      const value = editor.value;
      fileSaveInflight.add(filePath);
      renderBody();
      await window.Crew.writeTextFile(filePath, value);
      invalidateFilePreviewCache(filePath);
      fileContentCache.set(filePath, value);
    } else if (kind === 'docx' || kind === 'pptx') {
      const payload = fileBinaryCache.get(filePath);
      const edits = collectOfficeEditorBlockEdits(filePath);
      if (!payload?.base64) throw new Error('原 Office 文件未加载，无法保存');
      if (edits.length === 0) throw new Error('未找到 Office 编辑内容');
      if (!window.Crew?.writeFileBase64) throw new Error('当前环境不支持保存 Office 文件');
      fileSaveInflight.add(filePath);
      renderBody();
      const officeEdit = await import('../office-edit');
      let blocks = edits.map((edit) => edit.text);
      if (kind === 'pptx') {
        blocks = (await officeEdit.loadPptxEditBlocks(payload.base64))
          .map((block) => block.text);
        for (const edit of edits) blocks[edit.index] = edit.text;
      }
      const base64 = kind === 'docx'
        ? await officeEdit.patchDocxBlocks(payload.base64, blocks)
        : await officeEdit.patchPptxBlocks(payload.base64, blocks);
      await window.Crew.writeFileBase64(filePath, base64);
      invalidateFilePreviewCache(filePath);
      fileBinaryCache.set(filePath, {
        base64,
        mimeType: kind === 'docx'
          ? 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
          : 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
      });
    } else if (kind === 'xlsx') {
      const payload = fileBinaryCache.get(filePath);
      const root = document.querySelector<HTMLElement>(
        `[data-xlsx-editor="${CSS.escape(filePath)}"]`,
      );
      if (!payload?.base64) throw new Error('原 Excel 文件未加载，无法保存');
      if (!root) throw new Error('未找到 Excel 编辑器');
      if (!window.Crew?.writeFileBase64) throw new Error('当前环境不支持保存 Excel 文件');
      const { collectXlsxEditorPatch } = await import('../xlsx-editor');
      const patch = collectXlsxEditorPatch(root);
      if (!patch) throw new Error('Excel 编辑状态已失效，请重新打开编辑模式');
      fileSaveInflight.add(filePath);
      renderBody();
      const { patchXlsxGrid } = await import('../office-edit');
      const base64 = await patchXlsxGrid(payload.base64, patch);
      await window.Crew.writeFileBase64(filePath, base64);
      invalidateFilePreviewCache(filePath);
      fileBinaryCache.set(filePath, {
        base64,
        mimeType: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
      });
    } else {
      throw new Error('此文件类型暂不支持看板内编辑');
    }
    fileEditMode.delete(filePath);
    notify('已保存文件');
  } catch (error) {
    notify(`保存失败：${error instanceof Error ? error.message : String(error)}`);
  } finally {
    fileSaveInflight.delete(filePath);
    renderBody();
  }
}

function bindBodyEvents(): void {
  document.querySelectorAll<HTMLElement>('[data-file-toggle]').forEach((el) => {
    el.addEventListener('click', () => {
      const card = el.closest('.inspector-file') as HTMLElement | null;
      const path = card?.getAttribute('data-file-path');
      if (path) {
        const opened = toggleFileExpanded(path);
        renderBody();
        if (opened) {
          ensureInspectorWidthForFileDiff();
          void hydrateFileDiffIfNeeded(path);
          void hydrateFileContentIfNeeded(path);
        }
      }
    });
  });
  document.querySelectorAll<HTMLElement>('[data-file-reveal]').forEach((el) => {
    el.addEventListener('click', (event) => {
      event.preventDefault();
      event.stopPropagation();
      const targetPath = el.getAttribute('data-file-reveal');
      if (targetPath) void showFileOpenMenu(el, targetPath);
    });
  });
  document.querySelectorAll<HTMLButtonElement>('[data-file-view-toggle]').forEach((element) => {
    element.addEventListener('click', (event) => {
      event.preventDefault();
      event.stopPropagation();
      const filePath = element.getAttribute('data-file-view-toggle');
      if (!filePath) return;
      const current = fileViewMode.get(filePath) ?? 'preview';
      fileViewMode.set(filePath, current === 'preview' ? 'code' : 'preview');
      renderBody();
      if (fileViewMode.get(filePath) === 'preview') {
        void hydrateFileContentIfNeeded(filePath);
      }
    });
  });
  document.querySelectorAll<HTMLButtonElement>('[data-file-edit-toggle]').forEach((element) => {
    element.addEventListener('click', (event) => {
      event.preventDefault();
      event.stopPropagation();
      const filePath = element.getAttribute('data-file-edit-toggle');
      if (!filePath) return;
      if (fileEditMode.has(filePath)) fileEditMode.delete(filePath);
      else fileEditMode.add(filePath);
      renderBody();
      if (fileEditMode.has(filePath)) void hydrateFileContentIfNeeded(filePath);
    });
  });
  document.querySelectorAll<HTMLButtonElement>('[data-file-save]').forEach((element) => {
    element.addEventListener('click', (event) => {
      event.preventDefault();
      event.stopPropagation();
      const filePath = element.getAttribute('data-file-save');
      if (filePath) void saveFileEditor(filePath);
    });
  });
  // unmodified 折叠条：上下箭头每次揭开 20 行上下文。
  document.querySelectorAll<HTMLElement>('[data-diff-expand]').forEach((el) => {
    el.addEventListener('click', (event) => {
      event.preventDefault();
      event.stopPropagation();
      const edge = el.getAttribute('data-diff-expand');
      if (edge !== 'top' && edge !== 'bottom') return;
      const row = el.closest('[data-diff-region-start]') as HTMLElement | null;
      const card = el.closest('.inspector-file') as HTMLElement | null;
      const path = card?.getAttribute('data-file-path');
      const startRaw = row?.getAttribute('data-diff-region-start');
      if (!path || startRaw == null) return;
      const regionStart = Number(startRaw);
      if (!Number.isFinite(regionStart)) return;
      setDiffExpands(path, expandCollapsedRegion(getDiffExpands(path), regionStart, edge));
      renderBody();
    });
  });
  document.querySelectorAll<HTMLElement>('[data-msg-toggle]').forEach((el) => {
    el.addEventListener('click', () => {
      const card = el.closest('.inspector-msg') as HTMLElement | null;
      const id = card?.getAttribute('data-msg-id');
      if (id) {
        expandedMsg = expandedMsg === id ? null : id;
        renderBody();
      }
    });
  });
  bindPlanBoardEvents();
}

function readPlanBoardDraft(fallback = ''): string {
  const editor = document.querySelector<HTMLTextAreaElement>('[data-plan-editor]');
  if (editor) {
    planBoardUi.draft = editor.value;
    return editor.value;
  }
  return planBoardUi.draft ?? fallback;
}

function bindPlanBoardEvents(): void {
  document.querySelectorAll<HTMLElement>('[data-plan-mode]').forEach((el) => {
    el.addEventListener('click', () => {
      const mode = el.getAttribute('data-plan-mode');
      if (mode !== 'preview' && mode !== 'edit') return;
      capturePlanDraftFromDom();
      planBoardUi.mode = mode;
      renderBody();
    });
  });
  const editor = document.querySelector<HTMLTextAreaElement>('[data-plan-editor]');
  if (editor) {
    editor.addEventListener('input', () => {
      planBoardUi.draft = editor.value;
      planBoardUi.sessionId = state.activeSessionId;
    });
  }
  const otherInput = document.querySelector<HTMLTextAreaElement>('[data-plan-other-input]');
  if (otherInput) {
    otherInput.addEventListener('input', () => {
      planBoardUi.otherText = otherInput.value;
    });
  }
  document.querySelectorAll<HTMLElement>('[data-plan-board-action]').forEach((el) => {
    el.addEventListener('click', () => {
      void handlePlanBoardAction(el.getAttribute('data-plan-board-action') || '');
    });
  });
}

async function handlePlanBoardAction(action: string): Promise<void> {
  if (!action || planBoardUi.busy) return;
  const doc = getPendingPlanDoc();
  if (!doc || !isPlanActionable(doc.status)) return;
  const actions = planBoardActions;
  if (!actions) {
    notify('计划审批动作未就绪');
    return;
  }

  if (action === 'other') {
    planBoardUi.otherOpen = !planBoardUi.otherOpen;
    renderBody();
    if (planBoardUi.otherOpen) {
      document.querySelector<HTMLTextAreaElement>('[data-plan-other-input]')?.focus();
    }
    return;
  }

  if (action === 'reject_and_exit') {
    planBoardUi.busy = true;
    try {
      await actions.onRejectAndExit();
      planBoardUi.draft = null;
      planBoardUi.otherOpen = false;
      planBoardUi.otherText = '';
      planBoardUi.mode = 'preview';
    } finally {
      planBoardUi.busy = false;
      refreshInspector();
    }
    return;
  }

  if (action === 'approve') {
    const plan = readPlanBoardDraft(doc.plan).trim();
    if (!plan) {
      notify('计划为空，请先完善计划再批准');
      return;
    }
    planBoardUi.busy = true;
    try {
      await actions.onApprove(plan);
      planBoardUi.draft = null;
      planBoardUi.otherOpen = false;
      planBoardUi.otherText = '';
      planBoardUi.mode = 'preview';
    } finally {
      planBoardUi.busy = false;
      refreshInspector();
    }
    return;
  }

  if (action === 'submit_other') {
    const feedback = (document.querySelector<HTMLTextAreaElement>('[data-plan-other-input]')?.value
      ?? planBoardUi.otherText).trim();
    if (!feedback) {
      notify('请先填写修改要求');
      planBoardUi.otherOpen = true;
      renderBody();
      document.querySelector<HTMLTextAreaElement>('[data-plan-other-input]')?.focus();
      return;
    }
    const plan = readPlanBoardDraft(doc.plan);
    planBoardUi.busy = true;
    try {
      await actions.onFeedback(plan, feedback);
      planBoardUi.otherOpen = false;
      planBoardUi.otherText = '';
      planBoardUi.mode = 'preview';
      // draft 保留到服务端回推 plan_review 后再被覆盖
    } finally {
      planBoardUi.busy = false;
      refreshInspector();
    }
  }
}

function setTab(tab: TabKey): void {
  syncInspectorSessionUi();
  if (tab === 'collaboration' && !isExternalTeamSession()) return;
  if (tab !== 'browser') openCoreTabs.add(tab);
  if (activeTab === 'browser' && tab !== 'browser') releaseUserBrowserControl();
  if (activeTab === 'kanban' && tab !== 'kanban') {
    disconnectKanbanObserver();
  }
  if (activeTab === 'collaboration' && tab !== 'collaboration') stopTeamCollaborationPolling();
  activeTab = tab;
  document.body.classList.toggle('browser-workbench-open', tab === 'browser' && inspectorOpen);
  if (tab !== 'browser') document.body.classList.remove('browser-workbench-maximized');
  renderTabs();
  renderBody();
  if (tab === 'files' && expandedFiles.size > 0) {
    ensureInspectorWidthForFileDiff();
    for (const path of expandedFiles) {
      void hydrateFileDiffIfNeeded(path);
      void hydrateFileContentIfNeeded(path);
    }
  }
  if (tab === 'collaboration' && state.activeSessionId) startTeamCollaborationPolling(state.activeSessionId);
}

/** 当前是否允许展开看板/检查器。专家团会话即使尚无消息也可打开任务 Tab。 */
function canOpenInspector(): boolean {
  if (!state.activeSessionId) return false;
  if (isDynamicKanbanSession(state.activeSessionId)) return true;
  if (isExternalTeamSession(state.activeSessionId)) return true;
  return hasConversationInfo();
}

/** 当前是否存在可显示的「对话信息」。无则不允许展开检查器。 */
function hasConversationInfo(): boolean {
  const sid = state.activeSessionId;
  if (!sid) return false;
  const msgs = state.messages[sid];
  // 没有会话、或没有任何消息，都视为「无对话信息」
  if (!msgs || msgs.length === 0) return false;
  // 只统计 user / assistant / toolCalls 这类真正有内容的条目；
  // 单纯 status/error 提示不构成「对话信息」。
  return msgs.some((m) => {
    if (m.role === 'user' || m.role === 'assistant') {
      return !!(m.content && m.content.trim())
        || (m.toolCalls?.length ?? 0) > 0
        || !!m.planReview;
    }
    return false;
  });
}

/** 看板 / 检查器仅「对话」子页可用，工作室视图下禁用。 */
function canShowInspectorUi(): boolean {
  return state.activeTab === 'chat' && !isStudioView();
}

/** 同步右侧「看板」入口可用态。 */
export function syncInspectorToggleState(): void {
  const collaborationTab = document.getElementById('ins-collaboration-tab') as HTMLButtonElement | null;
  if (collaborationTab) {
    const visible = isExternalTeamSession(state.activeSessionId);
    collaborationTab.hidden = !visible;
    collaborationTab.classList.toggle('is-hidden', !visible);
    collaborationTab.setAttribute('aria-hidden', String(!visible));
  }
  const toggle = document.getElementById('task-board-toggle') as HTMLButtonElement | null;
  if (!toggle) return;

  const isChat = state.activeTab === 'chat';
  const sid = state.activeSessionId;
  const canOpen = canOpenInspector();
  const showToggle = isChat && !!sid && canShowInspectorUi();

  toggle.hidden = !showToggle;
  toggle.classList.toggle('is-active', inspectorOpen);
  toggle.disabled = !canOpen;
  toggle.setAttribute('aria-disabled', String(!canOpen));
  toggle.setAttribute('aria-expanded', String(inspectorOpen && canOpen));
  toggle.title = canOpen
    ? `看板（上下文 / 文件 / 计划 / 任务${isExternalTeamSession(sid) ? ' / 协作' : ''}）`
    : '当前没有对话信息，先发起一次对话再打开看板';
  toggle.setAttribute('aria-label', toggle.title);

  const browserToggle = document.getElementById('browser-workbench-toggle') as HTMLButtonElement | null;
  if (browserToggle) {
    const browserVisible = isChat && canShowInspectorUi();
    const browserActive = inspectorOpen && activeTab === 'browser';
    browserToggle.hidden = !browserVisible;
    browserToggle.disabled = false;
    browserToggle.classList.toggle('is-active', browserActive);
    browserToggle.setAttribute('aria-expanded', String(browserActive));
    const running = document.getElementById('browser-workbench-status')?.classList.contains('is-running');
    browserToggle.setAttribute(
      'aria-label',
      `${browserActive ? '关闭' : '打开'}浏览器，${running ? '运行中' : '未运行'}`,
    );
  }
}

/** 展开看板并切换到指定 Tab。
 *  `expandFilePath`：切到 Files 时强制展开该文件 diff（消息卡「查看」入口用）。
 *  未指定路径时，若当前尚无展开项，则展开会话改动列表的第一项，避免打开后全是折叠态。 */
export function openInspectorToTab(
  tab: TabKey = 'context',
  options?: {
    expandFilePath?: string | null;
    filePaths?: string[] | null;
    fileChanges?: InspectorFileSummary[] | null;
  },
): void {
  syncInspectorSessionUi();
  if (!canShowInspectorUi()) return;
  if (!canOpenInspector() && !(tab === 'browser' && state.activeSessionId)) {
    closeInspector();
    syncInspectorToggleState();
    return;
  }
  if (activeTab === 'browser' && tab !== 'browser') releaseUserBrowserControl();
  inspectorOpen = true;
  customViewOpen = false;
  activeTab = tab;
  disableInspectorSurfaceAutoWidth();
  document.body.classList.remove('site-annotation-workbench-open');
  document.body.classList.remove('blueprint-surface-open');
  if (tab !== 'browser') openCoreTabs.add(tab);
  document.body.classList.toggle('browser-workbench-open', tab === 'browser');
  if (tab !== 'browser') document.body.classList.remove('browser-workbench-maximized');
  if (tab === 'files') {
    activeFileTabPath = null;
    if (!options?.expandFilePath) expandedFiles.clear();
    computeFileChanges();
    scopedFilePaths = options?.filePaths?.length
      ? Array.from(new Set(options.filePaths.filter((path) => typeof path === 'string' && path.trim()).map((path) => path.trim())))
      : null;
    scopedFileChanges = options?.fileChanges?.length
      ? options.fileChanges.filter((file) => file?.path).map((file) => ({
        path: file.path,
        name: file.name || file.path.split(/[\\/]/).pop() || file.path,
        added: file.added || 0,
        removed: file.removed || 0,
        status: file.status === 'added' || file.status === 'deleted' || file.status === 'modified' ? file.status : 'modified',
        diff: file.diff || [],
      }))
      : null;
    const wanted = options?.expandFilePath?.trim() || '';
    if (wanted) {
      ensureFileExpanded(wanted);
    }
  } else {
    scopedFilePaths = null;
    scopedFileChanges = null;
  }
  document.body.classList.add('inspector-open');
  $('#chat-inspector')?.classList.add('is-open');
  renderTabs();
  if (activeTab === 'context') void loadInspectorContext(state.activeSessionId);
  if (activeTab === 'collaboration' && state.activeSessionId) {
    startTeamCollaborationPolling(state.activeSessionId);
  }
  renderBody();
  if (activeTab === 'files' && expandedFiles.size > 0) {
    ensureInspectorWidthForFileDiff();
    for (const path of expandedFiles) {
      void hydrateFileDiffIfNeeded(path);
      void hydrateFileContentIfNeeded(path);
    }
  }
  syncInspectorToggleState();
}

/** 打开由功能模块提供内容的自定义看板，并保持看板自身的开关状态一致。 */
export function openInspectorCustomView(): boolean {
  if (!canShowInspectorUi() || !state.activeSessionId) return false;
  if (activeTab === 'browser') {
    releaseUserBrowserControl();
    hideBrowserPanelView();
  }
  if (activeTab === 'collaboration') stopTeamCollaborationPolling();
  inspectorOpen = true;
  customViewOpen = true;
  activeTab = 'context';
  document.body.classList.remove('browser-workbench-open', 'browser-workbench-maximized', 'inspector-workspace-maximized');
  document.body.classList.add('inspector-open');
  $('#chat-inspector')?.classList.add('is-open');
  syncInspectorToggleState();
  return true;
}

function openInspector(): void {
  openInspectorToTab(defaultInspectorTabForSession());
}

export function defaultInspectorTabForSession(
  sessionId: string | null | undefined = state.activeSessionId,
): TabKey {
  return isExternalTeamSession(sessionId)
    ? 'collaboration'
    : isDynamicKanbanSession(sessionId)
      ? 'kanban'
      : 'context';
}

/** Open the session-scoped Browser workbench independently of model tool disclosure. */
export function openBrowserWorkbench(
  options: { createTab?: boolean; url?: string } = {},
): void {
  if (!canShowInspectorUi()) return;
  const sessionId = state.activeSessionId || ensureComposerDraftSession();
  if (!sessionId) return;
  openInspectorToTab('browser');
  if (browserOpeningSession === sessionId) return;
  const revision = ++browserOpeningRevision;
  void prepareBrowserWorkbench(
    sessionId,
    options.createTab !== false,
    options.url?.trim() ?? '',
    revision,
  );
}

let browserOpeningSession = '';
let browserOpeningRevision = 0;

async function prepareBrowserWorkbench(
  sessionId: string,
  createTab: boolean,
  url: string,
  revision: number,
): Promise<void> {
  if (browserOpeningSession === sessionId) return;
  browserOpeningSession = sessionId;
  try {
    await backendApi.ensureSession(sessionId, {
      workspace_id: composerWorkspaceId(),
      title: '浏览器',
    });
    if (state.activeSessionId !== sessionId || revision !== browserOpeningRevision) return;
    // Browser-only use turns the renderer draft into a real session, keeping
    // backend ownership, cleanup and later model Browser tools on one identity.
    commitDraftSession(sessionId, '浏览器', '内置浏览器');
    syncBrowserPanelSession(sessionId);
    // `createTab: false` is the observer-only path used when the first
    // browser_use tool card auto-expands the workbench. The panel has already
    // subscribed through renderBody()/bindBrowserPanel(), including while no
    // tab exists. Calling openUserBrowser() here used to create a blank *user*
    // tab and switch the whole session to human mode, racing the AI's first
    // browser_navigate action.
    if (createTab) {
      if (url) await openUserBrowser(url, true, { confirmTakeover: true });
      else await openUserBrowser('', true);
    }
    // Auto-open (watching the AI) is passive: never create a human blank tab,
    // just subscribe and wait for the AI's first tab to appear.
    else await openUserBrowser('', false, { createIfEmpty: false });
  } catch (error) {
    notify(`浏览器启动失败：${(error as Error).message}`);
  } finally {
    if (browserOpeningSession === sessionId) browserOpeningSession = '';
  }
}

export function closeInspector(): void {
  browserOpeningRevision += 1;
  browserOpeningSession = '';
  if (activeTab === 'browser') releaseUserBrowserControl();
  if (activeTab === 'collaboration') stopTeamCollaborationPolling();
  inspectorOpen = false;
  customViewOpen = false;
  hideBrowserPanelView();
  document.body.classList.remove('inspector-open');
  document.body.classList.remove('browser-workbench-open', 'browser-workbench-maximized', 'inspector-workspace-maximized', 'inspector-surface-auto-width', 'site-annotation-workbench-open', 'blueprint-surface-open');
  $('#chat-inspector')?.classList.remove('is-open');
  syncInspectorToggleState();
}

/**
 * Detach the session-scoped Browser surface before activeSessionId changes.
 * This closes only the workbench UI; the old Crew session keeps its tabs and
 * can resume them when the user returns.
 */
export function prepareInspectorForSessionChange(): void {
  if (inspectorOpen && activeTab === 'browser') closeInspector();
  else hideBrowserPanelView();
  syncBrowserPanelSession(null);
}

// ─── 面板宽度：可拖拽 + 持久化 ──────────────────────────
const INSPECTOR_WIDTH_KEY = 'crew.inspector.width';
const INSPECTOR_MIN_WIDTH = 280;
/** 默认宽度（上下文/计划等 Tab） */
const INSPECTOR_DEFAULT_WIDTH = 320;
/** 展开文件 diff 时自动加宽到默认的 1.5 倍 */
const INSPECTOR_FILES_EXPANDED_WIDTH = 480;
/** 常规拖拽宽度上限；放大使用独立的工作区覆盖态。 */
const INSPECTOR_MAX_WIDTH = 1200;
const INSPECTOR_SURFACE_MIN_WIDTH = 560;

function effectiveMaxInspectorWidth(): number {
  const vwCap = Math.floor(window.innerWidth * 0.72);
  return Math.max(INSPECTOR_MIN_WIDTH, Math.min(INSPECTOR_MAX_WIDTH, vwCap));
}

function ensureInspectorWidthForFileDiff(): void {
  const cur = loadInspectorWidth();
  if (cur < INSPECTOR_FILES_EXPANDED_WIDTH) {
    applyInspectorWidth(INSPECTOR_FILES_EXPANDED_WIDTH, true);
  }
}

function clampWidth(w: number): number {
  if (!Number.isFinite(w)) return INSPECTOR_DEFAULT_WIDTH;
  return Math.max(INSPECTOR_MIN_WIDTH, Math.min(effectiveMaxInspectorWidth(), Math.round(w)));
}

function loadInspectorWidth(): number {
  try {
    const raw = localStorage.getItem(INSPECTOR_WIDTH_KEY);
    if (!raw) return INSPECTOR_DEFAULT_WIDTH;
    return clampWidth(parseInt(raw, 10));
  } catch {
    return INSPECTOR_DEFAULT_WIDTH;
  }
}

function applyInspectorWidth(width: number, persist: boolean): void {
  const w = clampWidth(width);
  setRuntimeToken(document.documentElement, '--mw-inspector-width', `${w}px`);
  setRuntimeToken(document.documentElement, '--inspector-width', `${w}px`);
  const handle = document.getElementById('chat-inspector-resize-handle');
  handle?.setAttribute('aria-valuemin', String(INSPECTOR_MIN_WIDTH));
  handle?.setAttribute('aria-valuemax', String(effectiveMaxInspectorWidth()));
  handle?.setAttribute('aria-valuenow', String(w));
  if (persist) {
    try {
      localStorage.setItem(INSPECTOR_WIDTH_KEY, String(w));
    } catch {
      /* quota / disabled */
    }
  }
}

function applyResponsiveSurfaceWidth(): void {
  const target = Math.max(INSPECTOR_SURFACE_MIN_WIDTH, Math.floor(window.innerWidth * 0.52));
  applyInspectorWidth(target, false);
}

export function enableInspectorSurfaceAutoWidth(): void {
  document.body.classList.add('inspector-surface-auto-width');
  applyResponsiveSurfaceWidth();
}

export function disableInspectorSurfaceAutoWidth(): void {
  if (!document.body.classList.contains('inspector-surface-auto-width')) return;
  document.body.classList.remove('inspector-surface-auto-width');
  applyInspectorWidth(loadInspectorWidth(), false);
}

function bindInspectorResize(): void {
  const handle = document.getElementById('chat-inspector-resize-handle');
  if (!handle) return;
  // 启动时立即恢复用户上次拖出来的宽度
  applyInspectorWidth(loadInspectorWidth(), false);

  let pointerId: number | null = null;
  let startX = 0;
  let startWidth = 0;

  const onMove = (e: PointerEvent): void => {
    if (pointerId !== e.pointerId) return;
    // 把手在面板左侧，向左拖 = 变宽
    const delta = startX - e.clientX;
    applyInspectorWidth(startWidth + delta, false);
  };

  const onUp = (e: PointerEvent): void => {
    if (pointerId !== e.pointerId) return;
    if (handle.hasPointerCapture(e.pointerId)) handle.releasePointerCapture(e.pointerId);
    pointerId = null;
    handle.classList.remove('is-dragging');
    document.body.classList.remove('inspector-resizing');
    // 手势结束时再持久化，避免拖拽过程中频繁写 localStorage
    try {
      const cur = parseInt(document.documentElement.style.getPropertyValue('--mw-inspector-width'), 10);
      if (!isNaN(cur)) localStorage.setItem(INSPECTOR_WIDTH_KEY, String(clampWidth(cur)));
    } catch {
      /* ignore */
    }
  };

  handle.addEventListener('pointerdown', (e: PointerEvent) => {
    if (e.button !== 0) return;
    const inspector = document.getElementById('chat-inspector');
    if (!inspector) return;
    document.body.classList.remove('inspector-surface-auto-width');
    pointerId = e.pointerId;
    handle.setPointerCapture(e.pointerId);
    startX = e.clientX;
    startWidth = inspector.getBoundingClientRect().width;
    handle.classList.add('is-dragging');
    document.body.classList.add('inspector-resizing');
    e.preventDefault();
  });
  handle.addEventListener('pointermove', onMove);
  handle.addEventListener('pointerup', onUp);
  handle.addEventListener('pointercancel', onUp);

  handle.addEventListener('keydown', (event) => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    document.body.classList.remove('inspector-surface-auto-width');
    const current = document.getElementById('chat-inspector')?.getBoundingClientRect().width
      || loadInspectorWidth();
    const next = event.key === 'Home' ? INSPECTOR_MIN_WIDTH
      : event.key === 'End' ? effectiveMaxInspectorWidth()
        : current + (event.key === 'ArrowLeft' ? 24 : -24);
    applyInspectorWidth(next, true);
  });

  // 双击复位到默认宽度
  handle.addEventListener('dblclick', () => {
    document.body.classList.remove('inspector-surface-auto-width');
    applyInspectorWidth(INSPECTOR_DEFAULT_WIDTH, true);
  });

  window.addEventListener('resize', () => {
    if (document.body.classList.contains('inspector-surface-auto-width')) {
      applyResponsiveSurfaceWidth();
      return;
    }
    const cur = parseInt(document.documentElement.style.getPropertyValue('--mw-inspector-width'), 10);
    if (Number.isFinite(cur) && cur > 0) {
      applyInspectorWidth(clampWidth(cur), false);
    }
  });
}

function toggleInspectorMaximized(): void {
  const maximized = document.body.classList.toggle('inspector-workspace-maximized');
  const button = document.getElementById('inspector-maximize');
  button?.setAttribute('aria-pressed', String(maximized));
  button?.setAttribute('aria-label', maximized ? '还原预览' : '放大预览');
  if (button) button.title = maximized ? '还原预览' : '放大预览';
  window.dispatchEvent(new CustomEvent('inspector:layout-changed', { detail: { maximized } }));
  window.dispatchEvent(new Event('resize'));
}

function bindInspector(): void {
  const inspectorRoot = document.getElementById('chat-inspector');
  if (inspectorRoot) mountInspectorShell(inspectorRoot);
  // 启动时读取已保存的「默认展开」偏好；只有在没有 saved setting（首次打开）
  // 或者 saved setting 显式为 false 时，才保持收起。
  try {
    const raw = localStorage.getItem('crew.settings');
    if (raw) {
      const obj = JSON.parse(raw);
      if (obj && obj.inspectorOpen === true && canOpenInspector()) {
        // 注意：刚启动时可能还没有 messages，等到 hydrateBackendState 完成
        // 后通过 messages:changed 事件再尝试一次（见下方事件监听）。
        openInspector();
      }
    }
  } catch {
    /* ignore */
  }
  syncInspectorToggleState();
  bindInspectorResize();
  const tabStrip = document.getElementById('chat-inspector-tabs');
  tabStrip?.addEventListener('click', (event) => {
    const target = event.target as HTMLElement;
    const close = target.closest<HTMLButtonElement>('[data-workspace-tab-close]');
    if (close) {
      event.stopPropagation();
      closeWorkspaceTab(close.dataset.workspaceTabClose || '');
      return;
    }
    const tab = target.closest<HTMLButtonElement>('[data-workspace-tab]');
    if (tab) {
      const id = tab.dataset.workspaceTab || '';
      if (!inspectorOpen && id.startsWith('core:')) openInspectorToTab(id.slice(5) as TabKey);
      else activateWorkspaceTab(id);
    }
  });
  tabStrip?.addEventListener('keydown', (event) => {
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
    const buttons = Array.from(tabStrip.querySelectorAll<HTMLButtonElement>('.chat-inspector__tab-select'));
    const current = buttons.indexOf(document.activeElement as HTMLButtonElement);
    if (current < 0 || buttons.length < 2) return;
    event.preventDefault();
    const delta = event.key === 'ArrowLeft' ? -1 : 1;
    buttons[(current + delta + buttons.length) % buttons.length]?.focus();
  });
  const newTabToggle = document.getElementById('inspector-new-browser-tab') as HTMLButtonElement | null;
  const pickerToggle = document.getElementById('inspector-tab-picker-toggle') as HTMLButtonElement | null;
  const pickerMenu = document.getElementById('inspector-tab-menu');
  const setWorkspaceMenuOpen = (open: boolean): void => {
    if (open) renderInspectorTabMenu();
    if (pickerMenu) pickerMenu.hidden = !open;
    newTabToggle?.setAttribute('aria-expanded', String(open));
    pickerToggle?.setAttribute('aria-expanded', String(open));
  };
  const toggleWorkspaceMenu = (mode: 'new' | 'open'): void => {
    if (mode === 'open' && pickerToggle?.hidden) return;
    const sameMode = workspaceMenuMode === mode;
    workspaceMenuMode = mode;
    setWorkspaceMenuOpen(Boolean(pickerMenu?.hidden) || !sameMode);
  };
  newTabToggle?.addEventListener('click', () => toggleWorkspaceMenu('new'));
  pickerToggle?.addEventListener('click', () => toggleWorkspaceMenu('open'));
  pickerMenu?.addEventListener('click', (event) => {
    const target = event.target as HTMLElement;
    const entry = target.closest<HTMLButtonElement>('[data-workspace-entry]');
    if (entry) {
      openWorkspaceEntry(entry.dataset.workspaceEntry || '');
      setWorkspaceMenuOpen(false);
      return;
    }
    const tab = target.closest<HTMLButtonElement>('[data-workspace-tab]');
    if (tab) {
      activateWorkspaceTab(tab.dataset.workspaceTab || '');
      setWorkspaceMenuOpen(false);
    }
  });
  document.addEventListener('click', (event) => {
    if (pickerMenu?.hidden) return;
    const target = event.target as HTMLElement;
    if (target.closest('#inspector-tab-menu, #inspector-new-browser-tab, #inspector-tab-picker-toggle')) return;
    setWorkspaceMenuOpen(false);
  });
  document.getElementById('inspector-maximize')?.addEventListener('click', () => {
    toggleInspectorMaximized();
  });
  window.addEventListener('resize', updateOpenTabMenuToggleVisibility);
  const toggleBtn = document.getElementById('task-board-toggle') as HTMLButtonElement | null;
  toggleBtn?.addEventListener('click', () => {
    if (!canOpenInspector()) {
      syncInspectorToggleState();
      return;
    }
    if (inspectorOpen && activeTab !== 'browser') closeInspector();
    else openInspector();
    window.dispatchEvent(new CustomEvent('inspector:button-toggled', { detail: { open: inspectorOpen } }));
  });
  document.getElementById('inspector-close')?.addEventListener('click', closeInspector);
  const browserToggle = document.getElementById('browser-workbench-toggle') as HTMLButtonElement | null;
  browserToggle?.addEventListener('click', () => {
    if (inspectorOpen && activeTab === 'browser') closeInspector();
    else openBrowserWorkbench();
  });
  window.addEventListener('browser-workbench:command', ((event: Event) => {
    const action = (event as CustomEvent<{ action?: string }>).detail?.action;
    if (action === 'close') closeInspector();
    if (action === 'open-existing') openBrowserWorkbench({ createTab: false });
    if (action === 'maximize') {
      toggleInspectorMaximized();
    }
  }) as EventListener);
  document.addEventListener('keydown', (e) => {
    if ((e.metaKey || e.ctrlKey) && e.shiftKey && e.key.toLowerCase() === 'b') {
      e.preventDefault();
      if (inspectorOpen && activeTab === 'browser') closeInspector();
      else openBrowserWorkbench();
      return;
    }
    if (e.key === 'Escape' && inspectorOpen) closeInspector();
  });
  // 会话/消息变化时刷新按钮可用态
  window.addEventListener('session:changing', prepareInspectorForSessionChange);
  window.addEventListener('session:changed', () => {
    resetFileDiffCache(state.activeSessionId);
    syncBrowserPanelSession(state.activeSessionId);
    if (isExternalTeamSession(state.activeSessionId) && state.activeSessionId) {
      void refreshTeamCollaborationBoard(state.activeSessionId);
    } else {
      stopTeamCollaborationPolling();
    }
    syncInspectorToggleState();
    if (!canOpenInspector() && !(activeTab === 'browser' && state.activeSessionId) && inspectorOpen) closeInspector();
  });
  window.addEventListener('team-collaboration:updated', ((event: Event) => {
    const sessionId = (event as CustomEvent<{ sessionId?: string }>).detail?.sessionId;
    if (sessionId !== state.activeSessionId) return;
    renderTabs();
    if (inspectorOpen && activeTab === 'collaboration') renderBody();
  }) as EventListener);
  window.addEventListener('messages:changed', () => {
    syncInspectorToggleState();
    if (!canOpenInspector() && !(activeTab === 'browser' && state.activeSessionId) && inspectorOpen) closeInspector();
  });
  // 会话级模型切换 → 重拉网关用量并刷新「上下文」页（供应商/模型/上下文限制随会话模型变化）
  window.addEventListener('session:model-changed', (ev) => {
    // 只响应当前活跃会话：wiki 内嵌等会话的模型事件与本面板无关，不白白重拉。
    const sid = (ev as CustomEvent<{ sessionId?: string }>).detail?.sessionId;
    if (sid && sid !== state.activeSessionId) return;
    void loadInspectorContext(state.activeSessionId);
  });
  // 设置中心里的「检查器开关」改了 —— 同步 UI 状态
  window.addEventListener('inspector:setting-changed', ((ev: Event) => {
    const detail = (ev as CustomEvent<{ open: boolean }>).detail;
    if (!detail) return;
    if (detail.open) {
      if (canOpenInspector() && !inspectorOpen) openInspector();
    } else {
      if (inspectorOpen) closeInspector();
    }
    syncInspectorToggleState();
  }) as EventListener);
  // 按钮点按 → 写设置
  window.addEventListener('inspector:button-toggled', ((ev: Event) => {
    const detail = (ev as CustomEvent<{ open: boolean }>).detail;
    if (!detail) return;
    try {
      const KEY = 'crew.settings';
      const raw = localStorage.getItem(KEY);
      const obj = raw ? JSON.parse(raw) : {};
      obj.inspectorOpen = !!detail.open;
      localStorage.setItem(KEY, JSON.stringify(obj));
    } catch {
      /* ignore quota / disabled */
    }
  }) as EventListener);
}

export function bindInspectorUi(): void {
  bindInspector();
}

export function isInspectorOpen(): boolean {
  return inspectorOpen;
}

/** 当前 inspector 页签（供 plan_review 等决定是否抢焦点）。 */
export function getInspectorActiveTab(): TabKey {
  return activeTab;
}

export function invalidateFileDiffCachePaths(paths: string[]): void {
  for (const p of paths) {
    fileDiffCache.delete(p);
    fileDiffHydrateInflight.delete(p);
    fileContentCache.delete(p);
    fileBinaryCache.delete(p);
    fileContentHydrateInflight.delete(p);
    fileContentErrors.delete(p);
  }
}

export function refreshInspector(): void {
  // 始终刷新（即便关闭）以便 tab 计数 / 状态准确；关闭时不渲染 body 节省开销。
  if (customViewOpen) {
    syncInspectorToggleState();
    return;
  }
  renderTabs();
  syncInspectorToggleState();
  if (inspectorOpen) renderBody();
}

/**
 * 仅刷新 tab 角标 / 开关态，不重建 body。
 * 供流式 renderChat 使用：避免每个 delta 把计划 MD / 文件 diff 的滚动位置冲掉。
 * 正文仍由 todo_updated / file_changes / plan_review / setUsageSnapshot 等路径全量 refresh。
 */
export function refreshInspectorChrome(): void {
  if (customViewOpen) {
    syncInspectorToggleState();
    return;
  }
  renderTabs();
  syncInspectorToggleState();
}
