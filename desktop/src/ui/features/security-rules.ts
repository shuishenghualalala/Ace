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
  additional_permissions?: {
    filesystem?: Array<{ root?: unknown; access?: unknown }>;
    network?: Array<{ host?: unknown; port?: unknown; protocol?: unknown; allow_private?: unknown }>;
    allow_local_binding?: unknown;
  };
}

export function formatSecurityRule(rule: SecurityRuleView): string {
  const command = Array.isArray(rule.argv_prefix) ? rule.argv_prefix.map(String).join(' ') : '';
  const target = String(rule.action_summary ?? '').trim()
    || command
    || String(rule.kind ?? '未知操作');
  const cwd = rule.cwd ? ` · 目录 ${rule.cwd}` : '';
  return `${rule.decision === 'deny' ? '拒绝' : '允许'} ${target}${cwd}`;
}

export function formatSecurityRulePermissions(rule: SecurityRuleView): string {
  const permissions = rule.additional_permissions;
  if (!permissions) return '';
  const lines: string[] = [];
  for (const entry of permissions.filesystem ?? []) {
    const access = String(entry.access ?? '') === 'read_write' ? '读写' : '只读';
    lines.push(`额外文件权限（${access}）：${String(entry.root ?? '')}`);
  }
  for (const entry of permissions.network ?? []) {
    const privateLabel = entry.allow_private === true ? '，允许私网地址' : '';
    lines.push(
      `额外网络权限：${String(entry.host ?? '')}:${String(entry.port ?? '')}`
      + `（${String(entry.protocol ?? '')}${privateLabel}）`,
    );
  }
  if (permissions.allow_local_binding === true) lines.push('额外权限：允许监听本地端口');
  return lines.join('\n');
}
