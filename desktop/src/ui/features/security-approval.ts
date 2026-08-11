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

export type ConversationSecurityMode = 'request_approval' | 'auto_review' | 'full_access';
export type SecurityApprovalChoice = 'once' | 'session' | 'always' | 'reject';

export const SECURITY_APPROVAL_CHOICES: readonly SecurityApprovalChoice[] = [
  'once',
  'session',
  'always',
  'reject',
];

/** toolbar chip 的三选一：value/label/desc 由本模块（类型所有者）统一提供。 */
export const SECURITY_MODE_OPTIONS: { value: ConversationSecurityMode; label: string; desc: string }[] = [
  { value: 'request_approval', label: '每次询问', desc: '执行命令或修改文件前都询问' },
  { value: 'auto_review', label: '替我审批', desc: '工作空间内自动执行，空间外扩权时询问' },
  { value: 'full_access', label: '完全访问权限', desc: '除永久禁止的破坏性操作外全部放行' },
];

export const FULL_ACCESS_CONFIRMATION =
  '完全访问权限会让命令继承当前登录用户的文件与网络权限，普通操作不再逐条询问；删除文件系统根目录、格式化磁盘、无条件清空数据库等永久禁止操作仍会被拦截。确定只对当前对话启用吗？';

export function modeLabel(mode: ConversationSecurityMode): string {
  if (mode === 'full_access') return '完全访问权限';
  if (mode === 'auto_review') return '替我审批';
  return '每次询问';
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
  if (riskClass === 'workspace_file_write') return '修改工作空间文件';
  if (riskClass === 'external_file_write') return '修改项目外文件';
  if (riskClass === 'public_network') return '访问公开网络';
  if (riskClass === 'private_network') return '访问私有网络';
  if (riskClass === 'external_agent_network') return '启动外部智能体并允许其访问模型服务';
  if (riskClass === 'dangerous_command') return '执行高风险命令';
  if (riskClass === 'shell_command') return '执行命令';
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
  const lines = [
    // gateway schema 吐的是 risk_class（非 risk_level）；两层都兜一层以防上游变动。
    `风险：${riskClassLabel(riskClass, toolName)}`,
    ...(intent ? [`用途：${intent}`] : []),
    `操作：${actionKindLabel(kind)}`,
  ];
  if (kind === 'exec') {
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
  const permissions = (request['additional_permissions'] ?? {}) as Record<string, unknown>;
  const filesystem = Array.isArray(permissions['filesystem']) ? permissions['filesystem'] : [];
  filesystem.forEach((value) => {
    const entry = value as Record<string, unknown>;
    const access = String(entry['access'] ?? '') === 'read_write' ? '读写' : '只读';
    lines.push(`额外文件权限（${access}）：${String(entry['root'] ?? '')}`);
  });
  const network = Array.isArray(permissions['network']) ? permissions['network'] : [];
  network.forEach((value) => {
    const entry = value as Record<string, unknown>;
    const privateLabel = entry['allow_private'] === true ? '，允许私网地址' : '';
    lines.push(
      `额外网络权限：${String(entry['host'] ?? '')}:${String(entry['port'] ?? '')}`
      + `（${String(entry['protocol'] ?? '')}${privateLabel}）`,
    );
  });
  if (permissions['allow_local_binding'] === true) lines.push('额外权限：允许监听本地端口');
  if (kind === 'exec' && request['preview']) lines.push(`申请说明：${String(request['preview'])}`);
  lines.push('授权只匹配上面显示的完整动作和额外权限；任一范围变化都会重新判断。');
  return lines.join('\n');
}

const sessionModes = new Map<string, ConversationSecurityMode>();
// 按项目记忆的新对话预设。full_access 可以在空白输入框里显式选择，但仅消费一次，
// 创建该对话后立即回退，避免后续新对话静默继承最高权限。
const nextConversationModes = new Map<string, ConversationSecurityMode>();

function presetForWorkspace(workspaceId: string): ConversationSecurityMode {
  return nextConversationModes.get(workspaceId) ?? 'request_approval';
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
 * full_access 只作用于当前会话；无活跃会话时作为一次性新对话预设保存。
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
    try {
      const result = await setMode({ workspaceId, sessionId: sid, mode });
      if (!result?.ok) {
        notify(`安全模式切换失败：${String((result?.body as { detail?: string })?.detail ?? '未知错误')}`);
        return false;
      }
    } catch (err) {
      notify(`安全模式切换失败：${String((err as Error)?.message ?? err)}`);
      return false;
    }
    // Gateway ACK 后才更新 renderer 状态，避免 UI 显示 full_access 但后端仍在 managed（或相反）。
    sessionModes.set(sid, mode);
    if (mode !== 'full_access') nextConversationModes.set(workspaceId, mode);
  } else {
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
      } catch (err) {
        notify(`安全模式初始化失败：${String((err as Error)?.message ?? err)}，已回退到逐次审批`);
        return 'request_approval';
      }
    }
  }
  sessionModes.set(sessionId, preset);
  if (preset === 'full_access') nextConversationModes.set(workspaceId, 'request_approval');
  return preset;
}

export function securityModeForSession(sessionId: string): ConversationSecurityMode {
  return sessionModes.get(sessionId) ?? 'request_approval';
}

function decisionLabel(decision: SecurityApprovalChoice, request: Record<string, unknown>): string {
  if (decision === 'once') return '（仅这一次）';
  if (decision === 'session') return '（本次对话）';
  if (decision === 'always') {
    const kind = String((request['action'] as Record<string, unknown> | undefined)?.['kind'] ?? '');
    return kind === 'exec' ? '（始终允许此命令及所列权限）' : '（始终允许此操作）';
  }
  return '';
}

export function bindSecurityApprovalUi(): () => void {
  const panel = $('#composer-approval-panel') as HTMLElement | null;
  const summary = $('#composer-approval-summary');
  const container = queryPrimaryComposer('.chat-input-container');
  const decisionButtons = Array.from(
    panel?.querySelectorAll<HTMLButtonElement>('[data-security-decision]') ?? [],
  );
  let visibleRequest: Record<string, unknown> | null = null;
  let polling = false;
  let submitting = false;
  // visibleRequest 存在时仍每 5s 重拉一次：后端会在超时后作废 pending 请求，
  // 若完全短路，UI 会一直显示已作废的陈旧请求（M1）。
  const REFETCH_INTERVAL_MS = 5000;
  let lastPollAt = 0;

  const showOverlay = (): void => {
    container?.classList.add('is-approving');
    panel?.setAttribute('aria-hidden', 'false');
  };
  const hideOverlay = (): void => {
    container?.classList.remove('is-approving');
    panel?.setAttribute('aria-hidden', 'true');
  };

  const poll = async (): Promise<void> => {
    if (polling || submitting || !state.activeSessionId || !window.Crew?.securityPending) return;
    // 已有可见请求时，节流到 5s 重拉一次以检测后端作废；无可见请求时每秒轮询照旧。
    if (visibleRequest && Date.now() - lastPollAt < REFETCH_INTERVAL_MS) return;
    polling = true;
    lastPollAt = Date.now();
    try {
      const workspaceId = state.sessions.find((item) => item.id === state.activeSessionId)?.workspaceId
        ?? state.currentWorkspaceId
        ?? 'default';
      const result = await window.Crew.securityPending({ workspaceId, sessionId: state.activeSessionId });
      const body = result?.body as { requests?: Array<Record<string, unknown>> } | undefined;
      const request = body?.requests?.[0];
      if (!result?.ok || !request) {
        // 之前有可见请求但现在拉不到了 -> 后端已作废或已处理，撤掉 overlay。
        if (visibleRequest) {
          visibleRequest = null;
          hideOverlay();
        }
        return;
      }
      visibleRequest = request;
      if (summary) summary.textContent = formatApprovalSummary(request);
      showOverlay();
    } catch {
      // Gateway unavailable is reported by the existing backend status guard.
    } finally {
      polling = false;
    }
  };

  decisionButtons.forEach((button) => {
    button.addEventListener('click', async () => {
      if (submitting || !visibleRequest || !state.activeSessionId) return;
      const decision = button.dataset['securityDecision'] as SecurityApprovalChoice;
      if (!SECURITY_APPROVAL_CHOICES.includes(decision)) return;
      // always 会持久保存上面展示的完整动作；shell wrapper 只按完整命令精确匹配，
      // 不允许用 pwsh/bash 固定前缀泛化为未来任意脚本。
      if (decision === 'always'
        && !window.confirm('「始终允许」会持久保存上面展示的完整动作和额外权限。动作或权限范围发生任何变化都会重新询问。确定要持久授权吗？')) {
        return;
      }
      const workspaceId = String(visibleRequest['workspace_id'] ?? 'default');
      const requestId = String(visibleRequest['request_id'] ?? '');
      const taskId = typeof visibleRequest['task_id'] === 'string' ? String(visibleRequest['task_id']) : '';
      const action = visibleRequest['action'] as { argv?: unknown[] } | undefined;
      const argvPrefix = (action?.argv ?? []).map(String);
      const sessionId = state.activeSessionId;
      submitting = true;
      decisionButtons.forEach((item) => { item.disabled = true; });
      try {
        // task_id 必须回传：Gateway decide 校验 request.task_id == ctx.task_id，
        // 缺失会被判为上下文不匹配返回 409，并触发 main 侧删除 nonce，之后所有按钮都报"已过期"。
        // alwaysArgvPrefix 仅在 exec 动作（argv 非空）时携带：文件/网络动作 argv 为空，
        // 带空数组会被 IPC schema 判为非法（must be a non-empty string array）并 reject，
        // 这正是历史 bug——文件类"始终允许"点了没反应、框还挂着（决策见变更记录）。
        const result = await window.Crew.securityDecide({
          workspaceId,
          sessionId,
          requestId,
          taskId,
          decision,
          ...(decision === 'always' && argvPrefix.length ? { alwaysArgvPrefix: argvPrefix } : {}),
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
          ? `✕ 已拒绝，本次操作未执行`
          : `✔ 已批准${decisionLabel(decision, visibleRequest)}`;
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
