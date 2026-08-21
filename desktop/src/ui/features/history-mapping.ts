/**
 * 后端历史 → 前端 ChatMessage 的纯映射（无 DOM / 无副作用）。
 *
 * 从 ui/index.ts 抽出（X2）：guessAttachmentType / parseAttachmentMarkers /
 * mapBackendHistoryItem 原样搬迁，行为等价。
 */

import {
  type Attachment,
  type BackendHistoryFileChange,
  type BackendHistoryItem,
} from '../backend-client';
import type { ChatMessage, MessageRole, ToolCallInfo, TurnFileChangeSummary } from '../chat-render';
import { isPlanDocumentPath } from '../plan-document-path';
import { newMessageId, state } from '../state';
import { sessionDisplayModelLabel, sessionMessageModelLabel } from './session-model';

export function makeSessionTitle(text: string): string {
  return text.trim().slice(0, 18) || '新对话';
}

const IMAGE_EXTENSIONS = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'svg']);
const BINARY_RESULT_EXTENSIONS = new Set([
  'ppt', 'pptx', 'doc', 'docx', 'xls', 'xlsx', 'pdf',
  'png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'zip',
]);
const ATTACHMENT_MARKER_RE = /^附件「([^」]+)」位于[：:]\s*(.+)$/gm;

/** 与 Inspector 一致：识别会改文件的工具名，供旧历史兜底推断。 */
function isFileWriteTool(name: string): boolean {
  if (name === 'file_write' || name === 'patch') return true;
  return /write|edit|patch|create/i.test(name);
}

function fileNameFromPath(path: string): string {
  const parts = path.split(/[\\/]/);
  return parts[parts.length - 1] || path;
}

function isBinaryResultPath(path: string): boolean {
  const ext = path.split('.').pop()?.toLowerCase() ?? '';
  return BINARY_RESULT_EXTENSIONS.has(ext);
}

/** 从旧历史的 terminal 命令/结果中恢复明确输出的文件路径。 */
function inferTerminalResultPaths(
  raw: string | undefined,
  command: unknown,
  status: string | undefined,
): string[] {
  if (status === 'error') return [];
  const sources = [command].filter((value): value is string => typeof value === 'string');
  if (raw) {
    try {
      const payload = JSON.parse(raw) as unknown;
      if (payload && typeof payload === 'object') {
        const result = payload as { success?: unknown; exit_code?: unknown; command?: unknown; output?: unknown };
        if (result.success !== true || (typeof result.exit_code === 'number' && result.exit_code !== 0)) return [];
        if (typeof result.command === 'string') sources.push(result.command);
        if (typeof result.output === 'string') sources.push(result.output);
      }
    } catch {
      sources.push(raw);
    }
  }
  const paths = new Set<string>();
  const extension = '(?:pptx?|docx?|xlsx?|pdf|png|jpe?g|gif|webp|bmp|zip)';
  const patterns = [
    new RegExp(`--output\\s+["']?(.+?\\.${extension})(?=["']|\\s*(?:$|\\n))`, 'gim'),
    new RegExp(`(?:Done:|Wrote\\s+.+?\\s+to)\\s*(/.+?\\.${extension})\\s*$`, 'gim'),
    new RegExp(`^[-dlrwxsStT+@.\\s0-9:]+\\s+(/.+?\\.${extension})\\s*$`, 'gim'),
  ];
  for (const source of sources) {
    for (const pattern of patterns) {
      for (const match of source.matchAll(pattern)) {
        const path = match[1]?.trim();
        if (path?.startsWith('/')) paths.add(path);
      }
    }
  }
  return Array.from(paths);
}

/**
 * 从历史 tool_calls 推断本轮文件改动（旧会话无 turn_file_changes 时的兜底）。
 * 只能还原路径；增删行数未知，填 0。
 */
export function inferTurnFileChangesFromToolCalls(
  toolCalls: NonNullable<BackendHistoryItem['tool_calls']> | undefined,
): TurnFileChangeSummary[] {
  if (!toolCalls?.length) return [];
  const byPath = new Map<string, TurnFileChangeSummary>();
  for (const tc of toolCalls) {
    if (isFileWriteTool(tc.name)) {
      const raw = tc.arguments?.path;
      const path = typeof raw === 'string' ? raw.trim() : '';
      if (!path || isPlanDocumentPath(path)) continue;
      byPath.set(path, {
        path,
        name: fileNameFromPath(path),
        added: 0,
        removed: 0,
        status: 'modified',
      });
    } else if (tc.name === 'terminal') {
      for (const path of inferTerminalResultPaths(tc.result, tc.arguments?.command, tc.status)) {
        if (isPlanDocumentPath(path)) continue;
        byPath.set(path, {
          path,
          name: fileNameFromPath(path),
          added: 0,
          removed: 0,
          status: 'added',
          ...(isBinaryResultPath(path) ? { binary: true } : {}),
        });
      }
    }
  }
  return Array.from(byPath.values());
}

/**
 * 历史回放：剔除「本会话新建又已不存在」的幽灵路径。
 * 有 turn_file_changes 时以落库为准；旧会话仅从 tool_calls 推断时，读盘确认仍存在才保留。
 * 必须用 pathExists（静默），禁止用 readTextFile 探测——后者会在主进程刷 ENOENT。
 */
export async function filterExistingTurnFileChanges(
  files: TurnFileChangeSummary[],
): Promise<TurnFileChangeSummary[]> {
  if (!files.length) return files;
  const out: TurnFileChangeSummary[] = [];
  for (const f of files) {
    // 已明确 deleted 的保留；added/modified 若磁盘不在则视为临时文件剔除
    if (f.status === 'deleted') {
      out.push(f);
      continue;
    }
    if (!window.Crew?.pathExists) {
      // 无静默探测能力时保守保留（避免误删）；开发态 / 单测应 mock pathExists
      out.push(f);
      continue;
    }
    try {
      if (await window.Crew.pathExists(f.path)) out.push(f);
      // pathExists=false：文件不存在 → 剔幽灵
    } catch {
      // 探测异常（权限/IPC）：保守保留，避免误删真实改动卡
      out.push(f);
    }
  }
  return out;
}

/** 把后端 turn_file_changes 规范成前端 TurnFileChangeSummary。 */
export function normalizeTurnFileChanges(
  raw: BackendHistoryFileChange[] | undefined,
): TurnFileChangeSummary[] | undefined {
  if (!raw?.length) return undefined;
  const out: TurnFileChangeSummary[] = [];
  for (const f of raw) {
    if (!f || typeof f.path !== 'string' || !f.path.trim()) continue;
    const path = f.path.trim();
    if (isPlanDocumentPath(path)) continue;
    const status =
      f.status === 'added' || f.status === 'deleted' || f.status === 'modified' ? f.status : 'modified';
    const normalized = {
      path,
      name: typeof f.name === 'string' && f.name.trim() ? f.name : fileNameFromPath(path),
      added: typeof f.added === 'number' && Number.isFinite(f.added) ? f.added : 0,
      removed: typeof f.removed === 'number' && Number.isFinite(f.removed) ? f.removed : 0,
      status,
      ...(f.binary ? { binary: true } : {}),
    } satisfies TurnFileChangeSummary;
    // 与实时 reducer 保持一致：空文本文件的 metadata-only added 不形成可读改动卡。
    if (!normalized.binary && normalized.status === 'added' && normalized.added === 0 && normalized.removed === 0) {
      continue;
    }
    out.push(normalized);
  }
  return out.length > 0 ? out : undefined;
}

export function guessAttachmentType(name: string): Attachment['type'] {
  const ext = name.split('.').pop()?.toLowerCase() ?? '';
  return IMAGE_EXTENSIONS.has(ext) ? 'image' : 'file';
}

export function parseAttachmentMarkers(content: string): { content: string; attachments: Attachment[] } {
  const attachments: Attachment[] = [];
  const cleaned = content
    .replace(ATTACHMENT_MARKER_RE, (match, name, path) => {
      attachments.push({ id: `att-${attachments.length}`, name, path, type: guessAttachmentType(name) });
      return '';
    })
    .replace(/\n{2,}/g, '\n')
    .trim();
  return { content: cleaned, attachments };
}

/** 历史只持久化 running/done/error；旧版瞬时 generating 统一按未完成的 running 回放。 */
function normalizeHistoryToolStatus(
  status?: string,
  result?: string,
  duration?: number,
): ToolCallInfo['status'] {
  if (status === 'error') return 'error';
  if (status === 'done' || Boolean(result) || duration != null) return 'done';
  if (status === 'running' || status === 'generating') return 'running';
  return 'done';
}

export function mapBackendHistoryItem(item: BackendHistoryItem, sessionId: string | null = state.activeSessionId): ChatMessage {
  const role: MessageRole =
    item.role === 'assistant' || item.role === 'user' || item.role === 'team_internal'
      ? item.role
      : item.role === 'error' ? 'error' : 'status';
  const base: ChatMessage = {
    id: newMessageId(role),
    role,
    content: item.content,
    timestamp: item.timestamp != null ? item.timestamp * 1000 : Date.now(),
    model: item.model
      ? sessionMessageModelLabel(sessionId, item.model)
      : sessionDisplayModelLabel(sessionId),
    turnStartedAt: item.turn_started_at != null ? item.turn_started_at * 1000 : undefined,
    turnDurationMs: item.turn_duration != null ? Math.round(item.turn_duration * 1000) : undefined,
    thinking: item.thinking,
    sourceSessionId: item.source_session_id,
    agentId: item.agent_id,
    agentName: item.agent_name,
    agentRole: item.agent_role,
    agentTone: item.agent_tone,
    isLeader: item.is_leader,
    eventType: item.event_type,
    nodeId: item.node_id,
    mentionFrom: item.mention_from,
    mentionTo: item.mention_to,
    mentionIntent: item.mention_intent,
    communicationKind: item.communication_kind,
    communicationStatus: item.communication_status,
    requestId: item.request_id,
    replyTo: item.reply_to,
    communicationRequestText: item.communication_request_text,
    displayMode: item.display_mode,
    collapsedTitle: item.collapsed_title,
    processText: item.process_text,
    artifacts: item.artifacts,
    segmentRole: item.tool_calls?.length ? 'process' : 'answer',
    toolCalls: item.tool_calls?.map((tc) => ({
      toolCallId: tc.id,
      name: tc.name,
      uiLabel: tc.ui_label,
      args: JSON.stringify(tc.arguments),
      result: tc.result,
      status: normalizeHistoryToolStatus(tc.status, tc.result, tc.duration),
      startedAt: tc.started_at != null ? tc.started_at * 1000 : 0,
      duration: tc.duration != null ? Math.round(tc.duration * 1000) : 0,
    })),
  };
  if (role === 'assistant' || role === 'team_internal') {
    // 落库摘要保留准确 +/-；terminal 结果用于补充未落库的最终文件。
    const persisted = normalizeTurnFileChanges(item.turn_file_changes);
    const inferred = inferTurnFileChangesFromToolCalls(
      persisted ? item.tool_calls?.filter((tc) => tc.name === 'terminal') : item.tool_calls,
    );
    const merged = new Map<string, TurnFileChangeSummary>();
    for (const file of persisted ?? []) merged.set(file.path, file);
    for (const file of inferred) if (!merged.has(file.path)) merged.set(file.path, file);
    if (merged.size > 0) {
      base.turnFileChanges = Array.from(merged.values());
      if (persisted?.length) base.turnFileChangesPersistedPaths = persisted.map((file) => file.path);
    }
  }
  if (role === 'user') {
    const parsed = parseAttachmentMarkers(item.content);
    base.content = parsed.content;
    base.attachments = parsed.attachments;
  }
  return base;
}

function mergeStreamingText(existing?: string, incoming?: string, append = false): string | undefined {
  const left = String(existing || '');
  const right = String(incoming || '');
  if (!right) return left || undefined;
  if (!left) return right;
  if (append) return `${left}${right}`;
  if (left === right || left.endsWith(right) || left.includes(right)) return left;
  if (right.startsWith(left) || right.includes(left)) return right;
  let overlap = Math.min(left.length, right.length);
  while (overlap > 0 && left.slice(-overlap) !== right.slice(0, overlap)) overlap -= 1;
  return `${left}${right.slice(overlap)}`;
}

function mergeToolCalls(existing?: ToolCallInfo[], incoming?: ToolCallInfo[]): ToolCallInfo[] | undefined {
  const merged = new Map<string, ToolCallInfo>();
  for (const tool of existing || []) merged.set(tool.toolCallId, tool);
  for (const tool of incoming || []) {
    const previous = merged.get(tool.toolCallId);
    merged.set(tool.toolCallId, previous ? { ...previous, ...tool } : tool);
  }
  return merged.size ? Array.from(merged.values()) : undefined;
}

function isTeamNodeResult(message: ChatMessage): boolean {
  return ['team_submit', 'team_summary', 'team_review', 'team_decision'].includes(message.eventType || '');
}

function isTeamStream(message: ChatMessage): boolean {
  return message.eventType === 'team_stream';
}

function isTeamPlanningProgress(message: ChatMessage): boolean {
  return message.eventType === 'team_planning_progress';
}

function compactText(value?: string): string {
  return String(value || '').replace(/\s+/g, ' ').trim();
}

function isDuplicateAssistantOfTeamResult(existing: ChatMessage, incoming: ChatMessage): boolean {
  const assistant = existing.role === 'assistant' ? existing : incoming.role === 'assistant' ? incoming : null;
  const team = existing.role === 'team_internal' ? existing : incoming.role === 'team_internal' ? incoming : null;
  if (!assistant || !team || !isTeamNodeResult(team)) return false;
  if (assistant.requestId && team.requestId && assistant.requestId === team.requestId) {
    return team.eventType === 'team_summary';
  }
  const assistantText = compactText(assistant.content);
  const teamText = compactText(team.content);
  return Boolean(assistantText && teamText && (assistantText === teamText || assistantText.includes(teamText) || teamText.includes(assistantText)));
}

function teamTurnKey(message: ChatMessage): string {
  const source = String(message.sourceSessionId || '').trim();
  if (!source || !source.includes('::turn::')) return source;
  const [parent, rest = ''] = source.split('::turn::', 2);
  const requestId = rest.split('::', 1)[0];
  return requestId ? `${parent}::turn::${requestId}` : source;
}

function matchesTeamNode(existing: ChatMessage, incoming: ChatMessage): boolean {
  if (existing.role !== 'team_internal') return false;
  if (!isTeamStream(existing) && !isTeamNodeResult(existing) && !isTeamPlanningProgress(existing)) return false;
  if (incoming.nodeId && existing.nodeId === incoming.nodeId) {
    const existingTurn = teamTurnKey(existing);
    const incomingTurn = teamTurnKey(incoming);
    return Boolean(existingTurn && incomingTurn && existingTurn === incomingTurn);
  }
  return Boolean(
    !incoming.nodeId
    && existing.sourceSessionId
    && incoming.sourceSessionId
    && existing.sourceSessionId === incoming.sourceSessionId
    && existing.agentId === incoming.agentId,
  );
}

function isDuplicateTeamEvent(existing: ChatMessage, incoming: ChatMessage): boolean {
  return existing.role === 'team_internal'
    && incoming.role === 'team_internal'
    && !isTeamStream(existing)
    && !isTeamStream(incoming)
    && existing.eventType === incoming.eventType
    && existing.nodeId === incoming.nodeId
    && (existing.agentId || existing.mentionFrom) === (incoming.agentId || incoming.mentionFrom)
    && existing.sourceSessionId === incoming.sourceSessionId
    && existing.content.trim() === incoming.content.trim();
}

function isUserMentionAnswer(message: ChatMessage): boolean {
  return message.communicationKind === 'user_mention_answer' && Boolean(message.requestId);
}

function shouldSuppressApproveDecision(existing: ChatMessage, incoming: ChatMessage): boolean {
  const approved = incoming.eventType === 'team_decision'
    && (incoming.mentionIntent === 'approve' || ['审阅通过，继续后续流程。', '审阅通过，开始后续流程。'].includes(incoming.content.trim()));
  return approved && existing.eventType === 'team_review' && Boolean(existing.nodeId && existing.nodeId === incoming.nodeId);
}

function mergedTeamTurnTiming(existing: ChatMessage, incoming: ChatMessage): Pick<ChatMessage, 'turnStartedAt' | 'turnDurationMs'> {
  const turnStartedAt = existing.turnStartedAt ?? incoming.turnStartedAt;
  const persistedDuration = incoming.turnDurationMs ?? existing.turnDurationMs;
  const turnDurationMs = !incoming.streaming && turnStartedAt != null
    ? Math.max(0, persistedDuration ?? incoming.timestamp - turnStartedAt)
    : persistedDuration;
  return {
    ...(turnStartedAt != null ? { turnStartedAt } : {}),
    ...(turnDurationMs != null ? { turnDurationMs } : {}),
  };
}

/** 与 Web teamMessageMerge 同语义：同节点流式过程合并，节点结果替换过程卡，并去掉重复总答。 */
export function mergeTeamInternalMessage(
  messages: ChatMessage[],
  incoming: ChatMessage,
  options: { append?: boolean } = {},
): ChatMessage[] {
  if (incoming.role !== 'team_internal') return [...messages, incoming];
  const next = isTeamNodeResult(incoming)
    ? messages.filter((message) => !isDuplicateAssistantOfTeamResult(message, incoming))
    : messages;
  if (next.some((message) => isDuplicateTeamEvent(message, incoming))) return next;
  if (isUserMentionAnswer(incoming)) {
    const communicationIndex = next.findIndex((message) =>
      isUserMentionAnswer(message)
      && message.requestId === incoming.requestId
      && message.agentId === incoming.agentId,
    );
    if (communicationIndex >= 0) {
      const matched = next[communicationIndex];
      if (isTeamStream(incoming) && !isTeamStream(matched)) {
        return [
          ...next.slice(0, communicationIndex),
          { ...matched, ...incoming },
          ...next.slice(communicationIndex + 1),
        ];
      }
      if (isTeamStream(incoming)) {
        // 流式 direct mention 继续走下面的 append 合并，不能被 waiting 帧直接覆盖。
        // 终态则保留已收集的思考与工具过程，统一收口到同一个回答气泡。
      } else {
        const processText = isTeamStream(matched)
          && matched.content.trim()
          && matched.content.trim() !== incoming.content.trim()
          ? (matched.processText || matched.content)
          : incoming.processText;
        return [
          ...next.slice(0, communicationIndex),
          {
            ...matched,
            ...incoming,
            displayMode: incoming.displayMode || matched.displayMode,
            collapsedTitle: matched.collapsedTitle || incoming.collapsedTitle,
            processText,
            thinking: mergeStreamingText(matched.thinking, incoming.thinking),
            toolCalls: mergeToolCalls(matched.toolCalls, incoming.toolCalls),
            ...mergedTeamTurnTiming(matched, incoming),
          },
          ...next.slice(communicationIndex + 1),
        ];
      }
    }
  }
  let matchingIndex = -1;
  for (let index = next.length - 1; index >= 0; index -= 1) {
    if (matchesTeamNode(next[index], incoming)) {
      matchingIndex = index;
      break;
    }
  }
  if (isTeamPlanningProgress(incoming) && matchingIndex >= 0) {
    const matched = next[matchingIndex];
    return [
      ...next.slice(0, matchingIndex),
      { ...matched, ...incoming, ...mergedTeamTurnTiming(matched, incoming) },
      ...next.slice(matchingIndex + 1),
    ];
  }
  if (isTeamNodeResult(incoming) && matchingIndex >= 0) {
    const matched = next[matchingIndex];
    if (shouldSuppressApproveDecision(matched, incoming)) return next;
    if (!isTeamStream(matched)) return [...next, incoming];
    const incomingText = incoming.content.trim();
    const matchedProcess = (matched.processText || matched.content).trim();
    const incomingProcess = String(incoming.processText || '').trim();
    const processText = matchedProcess && matchedProcess !== incomingText
      ? (matched.processText || matched.content)
      : incomingProcess && incomingProcess !== incomingText ? incoming.processText : undefined;
    const replacement: ChatMessage = {
      ...matched,
      ...incoming,
      displayMode: incoming.displayMode || 'chat',
      collapsedTitle: matched.collapsedTitle || incoming.collapsedTitle,
      processText,
      thinking: mergeStreamingText(matched.thinking, incoming.thinking),
      toolCalls: mergeToolCalls(matched.toolCalls, incoming.toolCalls),
      ...mergedTeamTurnTiming(matched, incoming),
    };
    return [...next.slice(0, matchingIndex), replacement, ...next.slice(matchingIndex + 1)];
  }
  if (
    options.append
    && matchingIndex >= 0
    && isTeamStream(incoming)
    && isTeamStream(next[matchingIndex])
    && ['collapsible', 'stream'].includes(next[matchingIndex].displayMode || '')
  ) {
    const matched = next[matchingIndex];
    const replacement: ChatMessage = {
      ...matched,
      // Team provider 既可能发送 token delta，也可能发送截至当前的累计文本。
      // 复用单 Agent 的 overlap-safe 合并语义，避免累计 thinking 被逐帧重复拼接。
      content: mergeStreamingText(matched.content, incoming.content) || '',
      thinking: mergeStreamingText(matched.thinking, incoming.thinking),
      toolCalls: mergeToolCalls(matched.toolCalls, incoming.toolCalls),
      processText: mergeStreamingText(matched.processText, incoming.processText),
      turnStartedAt: matched.turnStartedAt ?? incoming.turnStartedAt,
      turnDurationMs: incoming.turnDurationMs ?? matched.turnDurationMs,
      streaming: incoming.streaming,
      timestamp: incoming.timestamp,
    };
    return [...next.slice(0, matchingIndex), replacement, ...next.slice(matchingIndex + 1)];
  }
  return [...next, incoming];
}

export function mergeTeamInternalMessages(messages: ChatMessage[]): ChatMessage[] {
  return messages.reduce(
    (result, message) => message.role === 'team_internal'
      ? mergeTeamInternalMessage(result, message, { append: message.eventType === 'team_stream' })
      : result.some((existing) => isDuplicateAssistantOfTeamResult(existing, message)) ? result : [...result, message],
    [] as ChatMessage[],
  );
}
