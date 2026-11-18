/**
 * 会话安全模式选择 + 单条命令批准 overlay（codex 式：盖在输入框区域上层）。
 *
 * Gateway 主动推送只负责唤醒，/security/pending 仍是 nonce 获取和断线兜底来源。
 * 命中后把「居中 modal」改成「输入框区域上层平滑遮盖」，并把决定结果写回
 * 对话流。模式选择器已从 header 下拉迁到对话框 toolbar chip。
 */

import type { SecurityDecisionArgs } from '../../shared/ipc-schemas';
import { interactiveOwnerKey } from './interactive-owner';
import { finishDecision, pendingDecisions, receiptStatus, rememberDecision, type PendingDecision } from './security-decision-receipts';
import { $, notify, state } from '../state';
import { sessionStore } from '../stores/session-store';
import { appendMessage, renderChat } from './chat-controller';
import { findChannelSession } from './channel-sessions';
import { queryPrimaryComposer } from './composer-scope';
import { defaultConversationSecurityMode, securityModuleEnabled, type ConversationSecurityMode } from './security-mode';
import { FULL_ACCESS_CHOICE_CHANGED, fullAccessChoiceEnabled, preferredNewConversationMode } from './security-permission-settings';
import { composerWorkspaceId, ensureComposerDraftSession, isDraftSession } from './workspaces';

export type { ConversationSecurityMode } from './security-mode';
export type SecurityApprovalChoice = 'once' | 'session' | 'always' | 'reject';

export const SECURITY_APPROVAL_CHOICES: readonly SecurityApprovalChoice[] = [
  'once',
  'session',
  'always',
  'reject',
];

/** toolbar chip 的三选一：value/label/desc 由本模块（类型所有者）统一提供。 */
export const SECURITY_MODE_OPTIONS: { value: ConversationSecurityMode; label: string; desc: string }[] = [
  { value: 'request_approval', label: '请求批准', desc: '沙箱内命令和工作区写入直接执行；扩权与高风险操作需确认' },
  { value: 'auto_review', label: '替我审批', desc: '沙箱内与公开网络自动放行，越界再问' },
  { value: 'full_access', label: '完全访问权限', desc: '使用当前宿主用户权限，不启用沙箱' },
];

export const FULL_ACCESS_CONFIRMATION =
  '完全访问会让当前对话直接使用你的宿主系统用户权限，不再启用原生文件或网络沙箱，也不逐项审批；永久安全红线、拒绝规则、审计与进程回收仍保留。确定启用吗？';

export const DRAFT_FULL_ACCESS_CONFIRMATION =
  '这条新对话将使用你的系统用户权限运行，关闭原生文件和网络沙箱及逐项审批。此选择只适用于这条对话。';

export function modeLabel(mode: ConversationSecurityMode): string {
  if (mode === 'full_access') return '完全访问权限';
  if (mode === 'auto_review') return '替我审批';
  return '请求批准';
}

function actionKindLabel(kind: string): string {
  if (kind === 'exec') return '执行命令';
  if (kind === 'file') return '文件操作';
  if (kind === 'network') return '联网访问';
  return kind || '需要人工确认';
}

function fileOperationLabel(operation: string): string {
  const map: Record<string, string> = { read: '读取', write: '写入', patch: '修改', delete: '删除' };
  return map[operation] ?? (operation || '未知操作');
}

function channelFileIntent(toolName: string): string {
  const map: Record<string, string> = {
    feishu_send_file: '发送文件到飞书',
  };
  return map[toolName] ?? '';
}

function riskClassLabel(riskClass: string, toolName: string): string {
  if (riskClass === 'external_file_read') {
    return channelFileIntent(toolName) ? '读取并发送项目外文件' : '读取项目外文件';
  }
  if (riskClass === 'external_file_write') return '修改项目外文件';
  if (riskClass === 'dangerous_command') return '执行高风险命令';
  if (riskClass === 'shell_command') return '执行命令';
  if (riskClass === 'external_agent_network') return '启动外部智能体并允许其访问模型服务';
  return riskClass || '需要人工确认';
}

/**
 * 把后端 pending 请求渲染成人类可读的审批摘要。
 *
 * 字段名必须与后端 NormalizedAction（crew/security/actions.py，经 asdict 下发）严格对齐：
 * exec→argv/cwd、file→path(单数)/operation、network→host/port/protocol。历史实现读的是
 * paths/network/additional_permissions 这三个后端根本不存在的键，导致文件/网络审批只显示
 * 「操作：file」却看不到具体路径或目标——用户无法据此做知情决策。此处按 kind 分支精确读取。
 */
export function formatApprovalSummary(request: Record<string, unknown>): string {
  const action = (request['action'] ?? {}) as Record<string, unknown>;
  const kind = String(action['kind'] ?? '');
  const toolName = String(request['tool_name'] ?? '');
  const intent = channelFileIntent(toolName);
  const riskClass = String(request['risk_class'] ?? action['risk_class'] ?? '');
  const argv = Array.isArray(action['argv']) ? action['argv'].map(String) : [];
  const additional = (request['additional_permissions'] ?? {}) as Record<string, unknown>;
  const sandboxPermissions = String(additional['sandbox_permissions'] ?? 'use_default');
  const lines = [
    // gateway schema 吐的是 risk_class（非 risk_level）；两层都兜一层以防上游变动。
    `风险：${riskClassLabel(riskClass, toolName)}`,
    ...(intent ? [`用途：${intent}`] : []),
    `操作：${actionKindLabel(kind)}`,
  ];
  if (kind === 'exec') {
    if (sandboxPermissions === 'require_escalated') {
      lines.push('执行边界：请求当前完整命令使用宿主用户权限');
      lines.push('警告：该命令将离开沙箱，可访问宿主用户能访问的文件和网络，包括 Ace 自身数据。');
    } else if (sandboxPermissions === 'with_additional_permissions') {
      lines.push('执行边界：留在沙箱内，并增加下方明确权限');
    } else {
      lines.push('执行边界：仅在当前沙箱内执行');
    }
    const rawCommand = String(action['raw_command'] ?? '');
    // raw_command 是用户/模型提交的原始命令，也是用户真正要确认的内容；argv 是系统最终执行
    // 详情（Windows 可能含 UTF-8 初始化前导）。两者都显示，避免内部前导淹没具体命令或隐藏执行差异。
    if (rawCommand) lines.push(`具体命令：${rawCommand}`);
    if (action['shell_kind']) lines.push(`命令环境：${String(action['shell_kind'])}`);
    const parsed = Array.isArray(action['parsed_commands']) ? action['parsed_commands'] : [];
    if (parsed.length) {
      lines.push(`解析结果：${parsed.map((command) => Array.isArray(command) ? command.map(String).join(' ') : String(command)).join('\n          ')}`);
    }
    if (argv.length) lines.push(`${rawCommand ? '最终执行参数' : '完整命令'}：${argv.join(' ')}`);
    if (action['cwd']) lines.push(`工作目录：${String(action['cwd'])}`);
    const proposedPrefix = Array.isArray(request['proposed_argv_prefix'])
      ? request['proposed_argv_prefix'].map(String)
      : [];
    if (proposedPrefix.length) lines.push(`可保存的命令前缀：${proposedPrefix.join(' ')}`);
  } else if (kind === 'file') {
    if (action['path']) lines.push(`文件：${String(action['path'])}`);
    lines.push(`文件操作：${fileOperationLabel(String(action['operation'] ?? ''))}`);
    if (request['preview']) lines.push(`变更预览：\n${String(request['preview'])}`);
  } else if (kind === 'network') {
    const host = String(action['host'] ?? '');
    const port = action['port'] ? `:${String(action['port'])}` : '';
    const protocol = action['protocol'] ? `（${String(action['protocol'])}）` : '';
    if (host) lines.push(`联网目标：${host}${port}${protocol}`);
  }
  const filesystem = Array.isArray(additional['filesystem']) ? additional['filesystem'] : [];
  const network = Array.isArray(additional['network']) ? additional['network'] : [];
  for (const value of filesystem) {
    const entry = value as Record<string, unknown>;
    const access = String(entry['access'] ?? '');
    lines.push(`额外文件权限（${access === 'read_write' ? '读写' : '只读'}）：${String(entry['root'] ?? '')}`);
  }
  for (const value of network) {
    const entry = value as Record<string, unknown>;
    lines.push(`额外网络权限：${String(entry['host'] ?? '')}:${String(entry['port'] ?? '')}（${String(entry['protocol'] ?? '')}）`);
  }
  if (additional['allow_local_binding'] === true) lines.push('额外权限：允许监听本地端口');
  if (request['preview'] && kind === 'exec') lines.push(`申请说明：${String(request['preview'])}`);
  lines.push(
    sandboxPermissions === 'require_escalated'
      ? '范围：仅这一次消费一次；本次对话仍绑定完整命令与工作目录；始终允许仅匹配完整动作。'
      : '范围：仅这一次消费一次；本次对话按已展示路径/网络范围复用；始终允许仅匹配完整动作。',
  );
  return lines.join('\n');
}

const sessionModes = new Map<string, ConversationSecurityMode>();
const browserDraftsAwaitingFirstTurn = new Map<string, { workspaceId: string; ownerKey: string }>();
let draftPermission: {
  sessionId: string;
  workspaceId: string;
  ownerKey: string;
  mode: ConversationSecurityMode;
} | null = null;

if (typeof window !== 'undefined') {
  window.addEventListener(FULL_ACCESS_CHOICE_CHANGED, () => {
    if (!fullAccessChoiceEnabled() && draftPermission?.mode === 'full_access') draftPermission = null;
  });
}

sessionStore.subscribe(({ activeSessionId }) => {
  if (draftPermission && activeSessionId !== draftPermission.sessionId) draftPermission = null;
});

function acknowledgedMode(
  result: { body?: unknown } | undefined,
): ConversationSecurityMode | null {
  const value = String((result?.body as { mode?: unknown } | undefined)?.mode ?? '');
  return value === 'request_approval' || value === 'auto_review' || value === 'full_access'
    ? value
    : null;
}

function announceModeChange(): void {
  if (typeof window !== 'undefined') {
    window.dispatchEvent(new CustomEvent('security:mode-change'));
  }
}

function workspaceIdForSession(sid: string | null | undefined): string {
  if (!sid || isDraftSession(sid)) return composerWorkspaceId();
  return (state.sessions.find((item) => item.id === sid) ?? findChannelSession(sid))?.workspaceId
    ?? 'default';
}

/** 已保存的对话显示已确认的模式，草稿显示单独选择或新对话默认值。 */
export function currentSecurityMode(): ConversationSecurityMode {
  const sid = state.activeSessionId;
  const workspaceId = workspaceIdForSession(sid);
  if (sid && !needsFirstTurnSecurityMode(sid, workspaceId)) {
    return securityModeForSession(sid);
  }
  return draftSecurityMode(sid, workspaceId);
}

export function canSelectFullAccess(sessionId = state.activeSessionId): boolean {
  if (!fullAccessChoiceEnabled()) return false;
  if (!sessionId || isDraftSession(sessionId)) return true;
  const row = state.sessions.find((item) => item.id === sessionId) ?? findChannelSession(sessionId);
  return Boolean(row?.workspaceId);
}

function securityOwnerKey(): string {
  const user = state.userInfo;
  return JSON.stringify([user?.uid, user?.pid, user?.staffCode, user?.staffUid]);
}

export function draftSecurityMode(
  sessionId: string | null,
  workspaceId: string,
): ConversationSecurityMode {
  const backendDefault = defaultConversationSecurityMode();
  if (state.config?.security?.enabled === false) return backendDefault;
  const selected = sessionId ? explicitDraftMode(sessionId, workspaceId) : undefined;
  if (selected) return selected;
  const preferred = preferredNewConversationMode();
  if (preferred === 'full_access') return fullAccessChoiceEnabled() ? preferred : 'auto_review';
  return preferred ?? (backendDefault === 'full_access' ? 'auto_review' : backendDefault);
}

function explicitDraftMode(sessionId: string, workspaceId: string): ConversationSecurityMode | undefined {
  if (draftPermission?.sessionId !== sessionId || draftPermission.workspaceId !== workspaceId
    || draftPermission.ownerKey !== securityOwnerKey()) return undefined;
  if (draftPermission.mode === 'full_access' && !fullAccessChoiceEnabled()) return undefined;
  return draftPermission.mode;
}

/** Browser can save the conversation before it has sent a model turn. */
export function rememberBrowserDraftSecurityScope(sessionId: string, workspaceId: string): void {
  if (isDraftSession(sessionId)) {
    browserDraftsAwaitingFirstTurn.set(sessionId, { workspaceId, ownerKey: securityOwnerKey() });
  }
}

export function needsFirstTurnSecurityMode(sessionId: string, workspaceId: string): boolean {
  if (isDraftSession(sessionId)) return true;
  const browserDraft = browserDraftsAwaitingFirstTurn.get(sessionId);
  return browserDraft?.workspaceId === workspaceId && browserDraft.ownerKey === securityOwnerKey();
}

export function clearDraftSecurityMode(sessionId: string): void {
  if (draftPermission?.sessionId === sessionId) draftPermission = null;
}

export function finishFirstTurnSecurityMode(sessionId: string): void {
  browserDraftsAwaitingFirstTurn.delete(sessionId);
  clearDraftSecurityMode(sessionId);
}

/**
 * 已保存的会话即时切换。草稿的权限在首条消息发送前提交给 Gateway。
 * 完全访问只绑定一次确认所指向的会话和工作空间。
 */
export async function selectNextConversationMode(
  mode: ConversationSecurityMode,
  confirmFullAccess: (isNewConversation: boolean) => boolean | Promise<boolean>,
): Promise<boolean> {
  const sid = state.activeSessionId;
  const workspaceId = workspaceIdForSession(sid);
  const draft = !sid || needsFirstTurnSecurityMode(sid, workspaceId);
  const ownerKey = securityOwnerKey();
  if (mode === 'full_access') {
    if (!canSelectFullAccess(sid) || !await confirmFullAccess(draft)) return false;
    if (state.activeSessionId !== sid || workspaceIdForSession(sid) !== workspaceId
      || securityOwnerKey() !== ownerKey || !canSelectFullAccess(sid)) return false;
  }
  if (draft) {
    const draftId = sid ?? ensureComposerDraftSession();
    if (!draftId || workspaceIdForSession(draftId) !== workspaceId) return false;
    draftPermission = { sessionId: draftId, workspaceId, ownerKey, mode };
  } else {
    const setMode = typeof window !== 'undefined' ? window.Crew?.securitySetMode : undefined;
    if (!setMode) {
      notify('安全模式切换失败：Desktop 安全桥不可用');
      return false;
    }
    let effectiveMode = mode;
    try {
      const result = await setMode({ workspaceId, sessionId: sid, mode });
      if (!result?.ok) {
        notify(`安全模式切换失败：${String((result?.body as { detail?: string })?.detail ?? '未知错误')}`);
        return false;
      }
      effectiveMode = acknowledgedMode(result) ?? mode;
    } catch (err) {
      notify(`安全模式切换失败：${String((err as Error)?.message ?? err)}`);
      return false;
    }
    // Gateway ACK 后才更新 renderer 状态，避免 UI 显示 full_access 但后端仍在 managed（或相反）。
    sessionModes.set(sid, effectiveMode);
  }
  announceModeChange();
  return true;
}

/** 首条消息前向 Gateway 提交新会话的权限，并等候确认。 */
export async function assignSecurityMode(
  sessionId: string,
  workspaceId = 'default',
  mode = draftSecurityMode(sessionId, workspaceId),
): Promise<ConversationSecurityMode | null> {
  const setMode = typeof window !== 'undefined' ? window.Crew?.securitySetMode : undefined;
  if (!securityModuleEnabled() && state.config?.security?.enabled === false) {
    const effective = defaultConversationSecurityMode();
    if (effective !== mode) return null;
    sessionModes.set(sessionId, effective);
    return effective;
  }
  if (!setMode && !securityModuleEnabled() && mode !== 'full_access') {
    sessionModes.set(sessionId, mode);
    return mode;
  }
  try {
    if (!setMode) throw new Error('Desktop 安全桥不可用');
    const result = await setMode({ workspaceId, sessionId, mode });
    if (!result?.ok || acknowledgedMode(result) !== mode) {
      throw new Error(String((result?.body as { detail?: string })?.detail ?? '网关未确认所选权限'));
    }
    sessionModes.set(sessionId, mode);
    return mode;
  } catch (error) {
    notify(`消息未发送，未能确认新对话权限：${String((error as Error)?.message ?? error)}`);
    return null;
  }
}

export async function revokeDraftFullAccess(sessionId: string, workspaceId: string): Promise<void> {
  sessionModes.delete(sessionId);
  try {
    await window.Crew?.securitySetMode?.({ workspaceId, sessionId, mode: 'request_approval' });
  } catch {
    // This draft cannot dispatch before the next successful permission acknowledgement.
  }
}

export function securityModeForSession(sessionId: string): ConversationSecurityMode {
  return sessionModes.get(sessionId) ?? defaultConversationSecurityMode();
}

function decisionLabel(decision: SecurityApprovalChoice): string {
  if (decision === 'once') return '（仅这一次）';
  if (decision === 'session') return '（本次对话）';
  if (decision === 'always') return '（始终允许此操作）';
  return '';
}

export function approvalBoundaryLabel(request: Record<string, unknown>): string {
  const additional = (request['additional_permissions'] ?? {}) as Record<string, unknown>;
  const sandboxPermissions = String(additional['sandbox_permissions'] ?? 'use_default');
  if (sandboxPermissions === 'require_escalated') return '脱离沙箱执行';
  const filesystem = Array.isArray(additional['filesystem']) ? additional['filesystem'] : [];
  if (filesystem.length) {
    const first = filesystem[0] as Record<string, unknown>;
    const access = String(first['access'] ?? '') === 'read_write' ? '读写' : '只读';
    const suffix = filesystem.length > 1 ? ` 等 ${filesystem.length} 项` : '';
    return `沙箱内增加${access}权限：${String(first['root'] ?? '')}${suffix}`;
  }
  const network = Array.isArray(additional['network']) ? additional['network'] : [];
  if (network.length || additional['allow_local_binding'] === true) return '沙箱内增加网络权限';
  return '仅在当前沙箱内执行';
}

// Wiki Agent 会话固定归属 workspace=wiki（与 wiki-agent.ts 的 WIKI_AGENT_WORKSPACE_ID 一致，
// 后端契约）；在独立 Wiki tab 里对话，state.activeSessionId 永远指向主聊天会话。
const WIKI_AGENT_WORKSPACE_ID = 'wiki';
// 轮询的会话上限：主聊天 + 本次运行打开过的少量 Wiki 会话，避免轮询扇出失控。
const MAX_POLL_SESSIONS = 5;

/**
 * 审批轮询覆盖的会话：主聊天活跃会话 + Wiki Agent 会话。
 *
 * Wiki 会话在独立视图里运行，activeSessionId 不会指向它；只看活跃会话时，wiki 回合里
 * 需要审批的工具调用（写文件、跑命令）在 UI 上无从批准，只能挂到 300s TTL 超时按拒绝
 * 处理（实测一次「生成 PDF」连撞三次审批墙，25 分钟耗在等待上）。
 */
function approvalPollCandidates(): Array<{ sessionId: string; workspaceId: string }> {
  const seen = new Set<string>();
  const candidates: Array<{ sessionId: string; workspaceId: string }> = [];
  const push = (sessionId: string | null | undefined, workspaceId: string | undefined): void => {
    const id = String(sessionId ?? '').trim();
    if (!id || seen.has(id)) return;
    seen.add(id);
    candidates.push({ sessionId: id, workspaceId: workspaceId ?? 'default' });
  };
  const active = state.activeSessionId;
  if (active) {
    push(
      active,
      state.sessions.find((item) => item.id === active)?.workspaceId
        ?? state.currentWorkspaceId
        ?? 'default',
    );
  }
  for (const row of state.sessions) {
    if (row.workspaceId === WIKI_AGENT_WORKSPACE_ID) push(row.id, row.workspaceId);
  }
  return candidates.slice(0, MAX_POLL_SESSIONS);
}

export function bindSecurityApprovalUi(): () => void {
  const panel = $('#composer-approval-panel') as HTMLElement | null;
  const summary = $('#composer-approval-summary');
  const container = queryPrimaryComposer('.chat-input-container');
  const decisionButtons = Array.from(
    panel?.querySelectorAll<HTMLButtonElement>('[data-security-decision]') ?? [],
  );
  // 面板在主 Composer 内的原始挂载位（approval 槽位）：全局浮动展示时要临时
  // 挂到 body 下，收回时需归位，否则主聊天的本地 overlay 模式会找不到面板。
  const panelHome = panel?.parentElement ?? null;
  let visibleRequest: Record<string, unknown> | null = null;
  let polling = false;
  let submitting = false;
  // visibleRequest 存在时仍每 5s 重拉一次：后端会在超时后作废 pending 请求，
  // 若完全短路，UI 会一直显示已作废的陈旧请求（M1）。
  const REFETCH_INTERVAL_MS = 5000;
  let lastPollAt = 0;

  const showOverlay = (request: Record<string, unknown>): void => {
    if (!panel) return;
    const requestSession = String(request['session_id'] ?? '').trim();
    const isActiveSession = Boolean(requestSession) && requestSession === state.activeSessionId;
    if (isActiveSession || !panelHome) {
      // 主聊天会话的审批：盖在其输入框上层（既有行为）。
      if (panel.parentElement !== panelHome) panelHome?.appendChild(panel);
      panel.classList.remove('composer-approval-panel--global');
      container?.classList.add('is-approving');
    } else {
      // 其他会话（如 Wiki 问答，独立 tab、主聊天输入框整体不可见）的审批：
      // 面板挂到 body 下浮动展示，否则会随隐藏的 tab 一起消失，
      // 用户永远看不到审批请求，工具只能干等 300s 超时按拒绝处理。
      container?.classList.remove('is-approving');
      if (panel.parentElement !== document.body) document.body.appendChild(panel);
      panel.classList.add('composer-approval-panel--global');
    }
    panel.setAttribute('aria-hidden', 'false');
  };
  const hideOverlay = (): void => {
    container?.classList.remove('is-approving');
    if (panel) {
      panel.classList.remove('composer-approval-panel--global');
      if (panelHome && panel.parentElement !== panelHome) panelHome.appendChild(panel);
    }
    panel?.setAttribute('aria-hidden', 'true');
  };

  let disposed = false;
  let nextReceiptCheck = 0;
  let checkingReceipts = false;
  let visibleOwner = '';
  const publishDecision = (sessionId: string, decision: SecurityApprovalChoice, boundary?: string): void => {
    const note = decision === 'reject' ? '✕ 已拒绝本次请求' : `✔ 已批准${decisionLabel(decision)}${boundary ? ' · ' + boundary : ''}`;
    appendMessage(sessionId, 'status', note);
    renderChat();
  };
  const settleReceipt = (entry: PendingDecision, receipt: unknown): boolean => {
    if (disposed || receiptStatus(receipt, entry) !== 'recorded' || !finishDecision(entry)) return false;
    publishDecision(entry.args.sessionId, entry.args.decision);
    return true;
  };
  const checkReceipt = async (entry: PendingDecision): Promise<void> => {
    if (entry.owner !== interactiveOwnerKey() || disposed) return;
    const reply = await window.Crew.securityDecisionReceipt(entry.args).catch(() => null);
    if (reply?.ok && entry.owner === interactiveOwnerKey()) settleReceipt(entry, reply.body);
  };

  const recoverDecisions = async (owner: string): Promise<void> => {
    if (!owner || checkingReceipts || Date.now() < nextReceiptCheck
        || typeof window.Crew?.securityDecisionReceipt !== 'function') return;
    checkingReceipts = true;
    try {
      const entries = pendingDecisions(owner);
      await Promise.all(entries.map(checkReceipt));
      nextReceiptCheck = Date.now() + (entries.length ? 15_000 : REFETCH_INTERVAL_MS);
    } finally {
      checkingReceipts = false;
    }
  };

  const poll = async (): Promise<void> => {
    if (disposed || polling || submitting || !window.Crew?.securityPending) return;
    const owner = interactiveOwnerKey();
    if (visibleOwner !== owner && visibleRequest) {
      visibleRequest = null;
      nextReceiptCheck = 0;
      hideOverlay();
    }
    // 已有可见请求时，节流到 5s 重拉一次以检测后端作废；无可见请求时每秒轮询照旧。
    if (visibleRequest && Date.now() - lastPollAt < REFETCH_INTERVAL_MS) return;
    const candidates = approvalPollCandidates();
    polling = true;
    lastPollAt = Date.now();
    try {
      void recoverDecisions(owner).catch(() => {});
      if (!candidates.length) return;
      // 后端 pending 按 (owner, workspace, session) 过滤，必须逐会话查询。
      const results = await Promise.all(
        candidates.map((candidate) =>
          window.Crew.securityPending({
            workspaceId: candidate.workspaceId,
            sessionId: candidate.sessionId,
          }).catch(() => null)),
      );
      if (disposed || submitting || owner !== interactiveOwnerKey()) return;
      let request: Record<string, unknown> | null = null;
      for (const result of results) {
        const body = result?.body as { requests?: Array<Record<string, unknown>> } | undefined;
        const first = body?.requests?.[0];
        if (result?.ok && first) {
          request = first;
          break;
        }
      }
      if (!request) {
        // 之前有可见请求但现在拉不到了 -> 后端已作废或已处理，撤掉 overlay。
        if (visibleRequest) {
          visibleRequest = null;
          hideOverlay();
        }
        return;
      }
      visibleRequest = request;
      visibleOwner = owner;
      if (summary) summary.textContent = formatApprovalSummary(request);
      showOverlay(request);
    } catch {
      // Gateway unavailable is reported by the existing backend status guard.
    } finally {
      polling = false;
    }
  };

  decisionButtons.forEach((button) => {
    button.addEventListener('click', async () => {
      if (disposed || submitting || !visibleRequest || visibleOwner !== interactiveOwnerKey()) return;
      const captured = visibleRequest;
      const owner = visibleOwner;
      const decision = button.dataset['securityDecision'] as SecurityApprovalChoice;
      if (!SECURITY_APPROVAL_CHOICES.includes(decision)) return;
      const proposedPrefix = Array.isArray(visibleRequest['proposed_argv_prefix'])
        ? visibleRequest['proposed_argv_prefix'].map(String)
        : [];
      if (decision === 'always'
        && !window.confirm(proposedPrefix.length
          ? `「始终允许」会在当前项目和工作目录保存命令前缀：${proposedPrefix.join(' ')}。后续匹配此前缀的命令会自动放行。确定要持久授权吗？`
          : '「始终允许」会持久保存上面展示的完整动作。只有命令、工作目录和执行参数完全一致时才会自动放行；任何变化都会重新询问。确定要持久授权吗？')) {
        return;
      }
      const workspaceId = String(visibleRequest['workspace_id'] ?? 'default');
      const requestId = String(visibleRequest['request_id'] ?? '');
      const taskId = typeof visibleRequest['task_id'] === 'string' ? String(visibleRequest['task_id']) : '';
      // 决策必须回传到发起请求的会话（pending 按 session 过滤、decide 校验上下文匹配），
      // 不能用主聊天活跃会话——Wiki 会话的审批用 activeSessionId 会被后端判为上下文不匹配。
      const sessionId = String(visibleRequest['session_id'] ?? '').trim() || state.activeSessionId;
      if (!sessionId) return;
      const command: SecurityDecisionArgs = {
        workspaceId, sessionId, requestId, taskId, decision,
        ...(decision === 'always' && proposedPrefix.length ? { alwaysArgvPrefix: proposedPrefix } : {}),
      };
      submitting = true;
      decisionButtons.forEach((item) => { item.disabled = true; });
      let entry: PendingDecision | undefined;
      try {
        if (captured['decision_receipts'] === 1 && typeof window.Crew.securityDecisionReceipt === 'function') {
          entry = await rememberDecision(command, owner);
          command.submissionId = entry.args.submissionId;
        }
        if (owner !== interactiveOwnerKey() || disposed) return;
        const result = await window.Crew.securityDecide(command);
        if (owner !== interactiveOwnerKey() || disposed) return;
        if (entry) {
          if (!result?.ok || !settleReceipt(entry, result.body?.receipt)) {
            await checkReceipt(entry);
          }
        } else if (result?.ok) {
          publishDecision(sessionId, decision, approvalBoundaryLabel(captured));
        } else {
          notify(`审批失败：${String(result?.body?.detail ?? '未知错误')}`);
        }
      } catch (err) {
        if (owner === interactiveOwnerKey() && !disposed) {
          if (entry) await checkReceipt(entry);
          else notify(`审批失败：${String((err as Error)?.message ?? err)}`);
        }
      } finally {
        if (entry && !disposed && owner === interactiveOwnerKey()
            && pendingDecisions(owner).some(row => row.args.submissionId === entry?.args.submissionId)) {
          notify('审批结果尚未确认，正在核对记录。');
        }
        visibleRequest = null;
        hideOverlay();
        lastPollAt = 0;
        submitting = false;
        decisionButtons.forEach((item) => { item.disabled = false; });
      }
    });
  });

  // 切会话：撤掉当前面板并丢弃旧请求；新会话若已有 pending，下个轮询周期会重新拉起。
  const onSessionChanged = (): void => {
    visibleRequest = null;
    hideOverlay();
  };
  window.addEventListener('session:changed', onSessionChanged);
  const onApprovalPending = (): void => {
    lastPollAt = 0;
    void poll();
  };
  window.addEventListener('security:approval-pending', onApprovalPending);

  const timer = window.setInterval(() => void poll(), 1000);
  return () => {
    disposed = true;
    window.clearInterval(timer);
    window.removeEventListener('session:changed', onSessionChanged);
    window.removeEventListener('security:approval-pending', onApprovalPending);
  };
}
