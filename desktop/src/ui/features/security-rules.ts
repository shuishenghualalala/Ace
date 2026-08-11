export interface SecurityRuleView {
  rule_id?: string;
  kind?: string;
  argv_prefix?: unknown[];
  cwd?: string;
  decision?: string;
  enabled?: boolean;
  exact_digest?: string;
  action_summary?: string;
  action_detail?: string;
}

export function formatSecurityRule(rule: SecurityRuleView): string {
  const command = Array.isArray(rule.argv_prefix) ? rule.argv_prefix.map(String).join(' ') : '';
  const target = String(rule.action_summary ?? '').trim()
    || command
    || String(rule.kind ?? '未知操作');
  const cwd = rule.cwd ? ` · 目录 ${rule.cwd}` : '';
  return `${rule.decision === 'deny' ? '拒绝' : '允许'} ${target}${cwd}`;
}
