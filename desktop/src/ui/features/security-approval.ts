/**
 * 会话安全模式选择 + 单条命令批准 overlay（codex 式：盖在输入框区域上层）。
 *
 * Gateway 主动推送只负责唤醒，/security/pending 仍是 nonce 获取和断线兜底来源。
 * 命中后把「居中 modal」改成「输入框区域上层平滑遮盖」，并把决定结果写回
 * 对话流。模式选择器已从 header 下拉迁到对话框 toolbar chip。
 */

import { $, notify, state } from '../state';
import { appendMessage, renderChat } from './chat-controller';
import { queryPrimaryComposer } from './composer-scope';

export type ConversationSecurityMode =
  | 'read_only'
  | 'request_approval'
  | 'auto_review'
  | 'full_access';
export type SecurityApprovalChoice = 'once' | 'session' | 'always' | 'reject';

export const SECURITY_APPROVAL_CHOICES: readonly SecurityApprovalChoice[] = [
  'once',
  'session',
  'always',
  'reject',
];

/** toolbar chip options: value/label/desc are owned by this module. */
export const SECURITY_MODE_OPTIONS: { value: ConversationSecurityMode; label: string; desc: string }[] = [
  { value: 'read_only', label: '只读', desc: '允许读取，文件写入始终拒绝' },
  { value: 'request_approval', label: '请求批准', desc: '每条命令都问我' },
  { value: 'auto_review', label: '替我审批', desc: '沙箱内自动放行，越界再问' },
  { value: 'full_access', label: '完全访问权限', desc: '宽权限受管：安全控制面仍隔离' },
];

export const FULL_ACCESS_CONFIRMATION =
  '完全访问会在原生安全运行时中开放当前项目和用户目录的广泛读写，普通动作不再逐条询问；Crew 数据库、授权规则、审计、凭据和系统级硬边界仍隔离。确定启用完全访问权限吗？';

export function modeLabel(mode: ConversationSecurityMode): string {
  if (mode === 'full_access') return '完全访问权限';
  if (mode === 'auto_review') return '替我审批';
  if (mode === 'read_only') return '只读';
  return '请求批准';
}

function actionKindLabel(kind: string): string {
  if (kind === 'exec') return '执行命令';
  if (kind === 'file') return '文件操作';
  if (kind === 'network') return '联网访问';
  if (kind === 'permission') return '权限申请';
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
 * exec→argv/cwd、file→path(单数)/operation、network→host/port/protocol；额外沙箱权限则
 * 从审批请求的 additional_permissions 读取。此处按 kind 分支精确读取，保证用户能做知情决策。
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
    const effective = request['effective_permissions'] as Record<string, unknown> | undefined;
    if (effective) {
      lines.push(`运行边界：${String(effective['kind']) === 'managed' ? '受管沙箱' : '当前用户权限'}`);
      const writeRoots = Array.isArray(effective['filesystem'])
        ? effective['filesystem']
          .map((item) => item as Record<string, unknown>)
          .filter((item) => String(item['access']) === 'read_write')
          .map((item) => String(item['root'] ?? ''))
          .filter(Boolean)
        : [];
      if (writeRoots.length) lines.push(`可写范围：${writeRoots.join('；')}`);
      const networkPolicy = String(effective['network_policy'] ?? '');
      if (networkPolicy) lines.push(`网络边界：${networkPolicy === 'unrestricted' ? '非受限' : '受限/显式目标'}`);
      const networkEntries = Array.isArray(effective['network']) ? effective['network'] : [];
      if (networkEntries.length) {
        const targets = networkEntries.map((item) => {
          const entry = item as Record<string, unknown>;
          return `${String(entry['protocol'] ?? '')}://${String(entry['host'] ?? '')}:${String(entry['port'] ?? '')}`;
        });
        lines.push(`网络目标：${targets.join('；')}`);
      }
    }
    const proposedPrefix = Array.isArray(request['proposed_argv_prefix'])
      ? request['proposed_argv_prefix'].map(String)
      : [];
    if (proposedPrefix.length) lines.push(`可保存的命令前缀：${proposedPrefix.join(' ')}`);
    lines.push('未知副作用：命令可能组合已授权文件/网络能力；审批只绑定显示的完整命令与运行边界。');
  } else if (kind === 'file') {
    if (action['path']) lines.push(`文件：${String(action['path'])}`);
    lines.push(`文件操作：${fileOperationLabel(String(action['operation'] ?? ''))}`);
    if (request['preview']) lines.push(`变更预览：\n${String(request['preview'])}`);
  } else if (kind === 'network') {
    const host = String(action['host'] ?? '');
    const port = action['port'] ? `:${String(action['port'])}` : '';
    const protocol = action['protocol'] ? `（${String(action['protocol'])}）` : '';
    if (host) lines.push(`联网目标：${host}${port}${protocol}`);
  } else if (kind === 'permission') {
    if (request['reason']) lines.push(`申请理由：${String(request['reason'])}`);
  }
  const extra = request['additional_permissions'] as Record<string, unknown> | undefined;
  const extraFilesystem = Array.isArray(extra?.['filesystem']) ? extra['filesystem'] : [];
  const extraNetwork = Array.isArray(extra?.['network']) ? extra['network'] : [];
  if (extraFilesystem.length || extraNetwork.length || extra?.['allow_local_binding'] === true) {
    lines.push('批准后额外沙箱权限：');
    for (const item of extraFilesystem) {
      const entry = item as Record<string, unknown>;
      lines.push(`  文件系统：${String(entry['root'] ?? '')}（${String(entry['access'] ?? 'read_write')}）`);
    }
    for (const item of extraNetwork) {
      const entry = item as Record<string, unknown>;
      const privateScope = entry['allow_private'] === true ? '（允许私网）' : '（仅公网）';
      lines.push(`  网络：${String(entry['protocol'] ?? '')}://${String(entry['host'] ?? '')}:${String(entry['port'] ?? '')}${privateScope}`);
    }
    if (extra?.['allow_local_binding'] === true) lines.push('  允许本地端口监听');
    lines.push('  额外权限仅绑定本次动作或本次对话，不会变成永久整机权限。');
  }
  lines.push(kind === 'permission'
    ? '授权仅覆盖上面列出的额外权限；未列出的能力仍会被拒绝。'
    : '授权只匹配上面显示的完整动作；任一字符变化都会重新判断。');
  if (request['preview'] && kind === 'exec') lines.push(`申请说明：${String(request['preview'])}`);
  return lines.join('\n');
}

const sessionModes = new Map<string, ConversationSecurityMode>();
// 按项目记忆的新对话预设（决策 #96）。full_access 永不作为预设持久化——
// 新对话必须再次确认（决策 #95），避免一次确认后静默继承最高权限。
const nextConversationModes = new Map<string, ConversationSecurityMode>();

function acknowledgedMode(
  result: { body?: unknown } | undefined,
  fallback: ConversationSecurityMode,
): ConversationSecurityMode {
  const value = String((result?.body as { mode?: unknown } | undefined)?.mode ?? '');
  return value === 'request_approval' || value === 'auto_review' || value === 'full_access'
    ? value
    : fallback;
}

function configuredDefaultMode(): ConversationSecurityMode {
  const configured = state.config?.security?.default_mode;
  return configured === 'request_approval' || configured === 'auto_review' || configured === 'full_access'
    ? configured
    : 'request_approval';
}

function presetForWorkspace(workspaceId: string): ConversationSecurityMode {
  return nextConversationModes.get(workspaceId) ?? configuredDefaultMode();
}

function announceModeChange(): void {
  if (typeof window !== 'undefined') {
    window.dispatchEvent(new CustomEvent('security:mode-change'));
  }
}

function workspaceIdForSession(sid: string | null | undefined): string {
  if (sid) {
    return state.sessions.find((item) => item.id === sid)?.workspaceId ?? 'default';
  }
  return state.currentWorkspaceId ?? 'default';
}

/** 当前生效模式：有活跃会话看会话绑定，否则看该项目的新对话预设。chip label 据此渲染。 */
export function currentSecurityMode(): ConversationSecurityMode {
  const sid = state.activeSessionId;
  if (sid) return securityModeForSession(sid);
  return presetForWorkspace(workspaceIdForSession(sid));
}

/**
 * 选择安全模式：若有活跃会话则即时切到该会话（toolbar chip 是「对话框内」选择器，
 * 与 codex 一致）。request_approval/auto_review 会记为该项目新对话预设并静默沿用；
 * full_access 只作用于当前会话，不写入预设——下次新对话仍需再次确认（决策 #95）。
 * full_access 的二次确认由调用方传入的 confirmFullAccess 负责。
 */
export async function selectNextConversationMode(
  mode: ConversationSecurityMode,
  confirmFullAccess: () => boolean | Promise<boolean>,
): Promise<boolean> {
  if (mode === 'full_access' && !await confirmFullAccess()) return false;
  const sid = state.activeSessionId;
  const workspaceId = workspaceIdForSession(sid);
  if (sid) {
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
      effectiveMode = acknowledgedMode(result, mode);
    } catch (err) {
      notify(`安全模式切换失败：${String((err as Error)?.message ?? err)}`);
      return false;
    }
    // Gateway ACK 后才更新 renderer 状态，避免 UI 显示 full_access 但后端仍在 managed（或相反）。
    sessionModes.set(sid, effectiveMode);
  }
  if (mode !== 'full_access') {
    nextConversationModes.set(workspaceId, mode);
  }
  announceModeChange();
  return true;
}

/** 新会话创建时（workspaces.ts）调用：把该项目的预设落到该会话绑定。
 *
 * 等 Gateway ACK 后才更新 renderer 本地状态，与 selectNextConversationMode 一致：
 * 否则前端可能显示 full_access 但后端仍在 managed（或相反），新会话首个动作就按错误
 * 模式授权。ACK 失败时不落 preset，保持安全默认 request_approval。
 */
export async function assignSecurityMode(
  sessionId: string,
  workspaceId = 'default',
): Promise<ConversationSecurityMode> {
  const preset = presetForWorkspace(workspaceId);
  if (typeof window !== 'undefined') {
    const setMode = window.Crew?.securitySetMode;
    if (setMode) {
      try {
        const result = await setMode({ workspaceId, sessionId, mode: preset });
        if (!result?.ok) {
          notify(`安全模式初始化失败：${String((result?.body as { detail?: string })?.detail ?? '未知错误')}，已回退到逐次审批`);
          return 'request_approval';
        }
        const effective = acknowledgedMode(result, preset);
        sessionModes.set(sessionId, effective);
        return effective;
      } catch (err) {
        notify(`安全模式初始化失败：${String((err as Error)?.message ?? err)}，已回退到逐次审批`);
        return 'request_approval';
      }
    }
  }
  sessionModes.set(sessionId, preset);
  return preset;
}

export function securityModeForSession(sessionId: string): ConversationSecurityMode {
  return sessionModes.get(sessionId) ?? configuredDefaultMode();
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

  const poll = async (): Promise<void> => {
    if (polling || submitting || !window.Crew?.securityPending) return;
    // 已有可见请求时，节流到 5s 重拉一次以检测后端作废；无可见请求时每秒轮询照旧。
    if (visibleRequest && Date.now() - lastPollAt < REFETCH_INTERVAL_MS) return;
    const candidates = approvalPollCandidates();
    if (!candidates.length) return;
    polling = true;
    lastPollAt = Date.now();
    try {
      // 后端 pending 按 (owner, workspace, session) 过滤，必须逐会话查询。
      const results = await Promise.all(
        candidates.map((candidate) =>
          window.Crew.securityPending({
            workspaceId: candidate.workspaceId,
            sessionId: candidate.sessionId,
          }).catch(() => null)),
      );
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
      const extra = request['additional_permissions'] as Record<string, unknown> | undefined;
      const hasExtraPermissions = Boolean(
        (Array.isArray(extra?.['filesystem']) && extra['filesystem'].length)
        || (Array.isArray(extra?.['network']) && extra['network'].length)
        || extra?.['allow_local_binding'] === true,
      );
      const requiresFreshConfirmation = request['risk_class'] === 'dangerous_command';
      const alwaysButton = panel?.querySelector<HTMLButtonElement>('[data-security-decision="always"]');
      if (alwaysButton) {
        alwaysButton.hidden = hasExtraPermissions || requiresFreshConfirmation;
        alwaysButton.disabled = hasExtraPermissions || requiresFreshConfirmation;
      }
      const sessionButton = panel?.querySelector<HTMLButtonElement>('[data-security-decision="session"]');
      if (sessionButton) {
        sessionButton.hidden = requiresFreshConfirmation;
        sessionButton.disabled = requiresFreshConfirmation;
      }
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
      if (submitting || !visibleRequest) return;
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
      const requestType = String(visibleRequest['request_type'] ?? 'action');
      const requestedPermissions = visibleRequest['permissions'] as Record<string, unknown> | undefined;
      // 决策必须回传到发起请求的会话（pending 按 session 过滤、decide 校验上下文匹配），
      // 不能用主聊天活跃会话——Wiki 会话的审批用 activeSessionId 会被后端判为上下文不匹配。
      const sessionId = String(visibleRequest['session_id'] ?? '').trim() || state.activeSessionId;
      if (!sessionId) return;
      submitting = true;
      decisionButtons.forEach((item) => { item.disabled = true; });
      try {
        // task_id 必须回传：Gateway decide 校验 request.task_id == ctx.task_id，
        // 缺失会被判为上下文不匹配返回 409，并触发 main 侧删除 nonce，之后所有按钮都报"已过期"。
        const result = await window.Crew.securityDecide({
          workspaceId,
          sessionId,
          requestId,
          taskId,
          decision,
          ...(decision === 'always' && proposedPrefix.length
            ? { alwaysArgvPrefix: proposedPrefix }
            : {}),
          ...(requestType === 'permission' && decision !== 'reject' && requestedPermissions
            ? { permissions: requestedPermissions }
            : {}),
        });
        if (!result?.ok) {
          notify(`审批失败：${String((result?.body as { detail?: string })?.detail ?? '未知错误')}`);
          // 失败时必须撤掉 overlay 并清空 visibleRequest，否则它会一直停在屏幕上，
          // 且后续每次点击都拿同一个陈旧 request_id 再失败（用户反馈的"框一直在 + 点啥都报已过期"）。
          visibleRequest = null;
          hideOverlay();
          lastPollAt = 0;
          return;
        }
        // 写回对话流：不带 activity 的 status 消息会持久保留（带 activity 的才被回合折叠/清掉）。
        const note = decision === 'reject'
          ? `✕ 已拒绝，命令未执行`
          : `✔ 已批准${decisionLabel(decision)} · ${approvalBoundaryLabel(visibleRequest)}`;
        appendMessage(sessionId, 'status', note);
        renderChat();
        visibleRequest = null;
        hideOverlay();
      } catch (err) {
        // securityDecide 走 Electron IPC：参数校验失败或桥接异常会让 invoke reject。
        // 此前回调无 try/catch，reject 会中断整个处理——decision 从未送达后端、overlay 不关、
        // visibleRequest 不清，工具一直挂到 300s 超时。兜底：提示 + 撤 overlay + 强制下次立即重拉。
        notify(`审批失败：${String((err as Error)?.message ?? err)}`);
        visibleRequest = null;
        hideOverlay();
        lastPollAt = 0;
      } finally {
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
    window.clearInterval(timer);
    window.removeEventListener('session:changed', onSessionChanged);
    window.removeEventListener('security:approval-pending', onApprovalPending);
  };
}
