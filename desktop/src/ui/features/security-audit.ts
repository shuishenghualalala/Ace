export interface SecurityAuditView {
  event_id?: string;
  timestamp?: number;
  action_type?: string;
  decision?: string;
  sandbox_backend?: string;
  stable_error_code?: string;
  request_id?: string;
  tool_name?: string;
  workspace_id?: string;
  workspace_name?: string;
  workspace_root?: string;
  session_id?: string;
  session_title?: string;
  task_id?: string;
  rule_scope?: string;
  decision_source?: string;
  action_summary?: string;
  action_detail?: string;
  additional_permissions_summary?: string;
  approval_mode?: string;
  current_approval_mode?: string;
}

export interface SecurityAuditQuery {
  actionType: '' | 'approval_requested' | 'approval_decision' | 'exec_decision'
    | 'file_decision' | 'network_decision' | 'exec_result' | 'rule_created'
    | 'rule_disabled' | 'rule_deleted' | 'audit_purged';
  decision: '' | 'allow' | 'deny' | 'pending' | 'ask' | 'once' | 'session'
    | 'always' | 'reject' | 'completed' | 'failed' | 'cancelled' | 'error'
    | 'enabled' | 'disabled' | 'deleted';
  sessionId: string;
  sort: 'newest' | 'oldest';
}

export const EMPTY_SECURITY_AUDIT_QUERY: SecurityAuditQuery = {
  actionType: '',
  decision: '',
  sessionId: '',
  sort: 'newest',
};

export function actionTypeLabel(value?: string): string {
  const labels: Record<string, string> = {
    approval_requested: '等待审批',
    approval_decision: '用户审批',
    exec_decision: '命令判定',
    file_decision: '文件判定',
    network_decision: '网络判定',
    exec_result: '命令执行结果',
    rule_created: '创建授权规则',
    rule_disabled: '停用授权规则',
    rule_deleted: '删除授权规则',
    audit_purged: '清理审计记录',
  };
  return labels[value ?? ''] ?? value ?? '安全事件';
}

export function approvalChoiceLabel(event: SecurityAuditView): string {
  const value = event.rule_scope || event.decision || '';
  const labels: Record<string, string> = {
    once: '仅一次',
    session: '当前会话',
    always: '始终允许',
    reject: '拒绝',
    allow: '允许',
    deny: '拒绝',
    pending: '等待审批',
    ask: '需要审批',
    completed: '执行成功',
    failed: '执行失败',
    cancelled: '已取消',
    error: '运行错误',
    enabled: '已启用',
    disabled: '已停用',
    deleted: '已删除',
  };
  return labels[value] || value || '—';
}

export function modeLabel(value?: string): string {
  const labels: Record<string, string> = {
    request_approval: '每次询问',
    auto_review: '替我审批',
    full_access: '完全访问权限',
  };
  return labels[value ?? ''] || value || '—';
}

export function decisionSourceLabel(value?: string): string {
  const labels: Record<string, string> = {
    desktop_user: '用户选择',
    gateway: '安全网关',
    immutable_policy: '不可变安全策略',
    recent_user_rejection: '近期用户拒绝',
    always_deny_rule: '永久拒绝规则',
    base_profile: '基础权限配置',
    always_rule: '永久授权规则',
    runtime_grant: '临时授权',
    full_access: '完全访问权限模式',
    auto_review: '自动审批',
    approval_required: '需要用户审批',
  };
  return labels[value ?? ''] || value || '—';
}

export function formatSecurityAudit(event: SecurityAuditView): string {
  const timestamp = event.timestamp ? new Date(event.timestamp * 1000).toLocaleString() : '未知时间';
  const backend = event.sandbox_backend ? ` · ${event.sandbox_backend}` : '';
  const error = event.stable_error_code ? ` · ${event.stable_error_code}` : '';
  const tool = event.tool_name ? ` · ${event.tool_name}` : '';
  const request = event.request_id ? ` · 请求 ${event.request_id.slice(0, 8)}` : '';
  const session = event.session_id ? ` · 会话 ${event.session_id}` : '';
  const summary = event.action_summary ? ` · ${event.action_summary}` : '';
  return `${timestamp} · ${event.action_type ?? 'security_event'} · ${event.decision ?? '—'}${tool}${request}${session}${summary}${backend}${error}`;
}
