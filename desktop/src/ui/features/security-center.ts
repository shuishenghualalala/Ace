/**
 * Security Center composition root.
 *
 * The renderer view owns presentation only. Existing preload security APIs and
 * security-approval mode semantics remain the sole execution path.
 */

import { $, notify, state } from '../state';
import { showConfirmDialog } from '../ui-feedback';
import {
  createSecurityCenterView,
  type SecurityCenterSnapshot,
  type SecurityCenterView,
} from './security-center-view';
import {
  FULL_ACCESS_CONFIRMATION,
  currentSecurityMode,
  selectNextConversationMode,
  type ConversationSecurityMode,
} from './security-approval';
import type { SecurityCapabilities } from './security-mode';
import type { SecurityRuleView } from './security-rules';
import { enableUacAndPromptRestart, prepareWindowsSecuritySetup } from './security-setup-flow';
import {
  EMPTY_SECURITY_AUDIT_QUERY,
  type SecurityAuditQuery,
  type SecurityAuditView,
} from './security-audit';

type GatewayResult = { ok?: boolean; body?: unknown };

let securityCenterView: SecurityCenterView | null = null;
let snapshot: SecurityCenterSnapshot = emptySnapshot();

function workspaceId(): string {
  return state.currentWorkspaceId ?? 'default';
}

function emptySnapshot(): SecurityCenterSnapshot {
  return {
    loading: false,
    setupAction: null,
    error: '',
    workspaceId: workspaceId(),
    strictSecurityEnabled: true,
    mode: currentSecurityMode(),
    capabilities: null,
    rules: [],
    audits: [],
    auditPage: 1,
    auditPageSize: 20,
    auditTotal: 0,
    auditQuery: { ...EMPTY_SECURITY_AUDIT_QUERY },
  };
}

function ensureSecurityCenter(): SecurityCenterView | null {
  const root = $('#security-page-root');
  if (!root) return null;
  if (!securityCenterView) {
    securityCenterView = createSecurityCenterView({
      onRefresh: () => void refresh(),
      onStrictSecurityChange: (enabled) => void changeStrictSecurity(enabled),
      onModeChange: (mode) => void changeMode(mode),
      onInstall: () => void setupProtection('repair'),
      onUninstall: () => void setupProtection('uninstall'),
      onRuleToggle: (rule) => void toggleRule(rule),
      onRuleDelete: (rule) => void deleteRule(rule),
      onAuditExport: () => void exportAudit(),
      onAuditPurge: () => void purgeAudit(),
      onAuditPageChange: (page) => void loadAuditPage(page, snapshot.auditPageSize ?? 20),
      onAuditPageSizeChange: (size) => void loadAuditPage(1, size),
      onAuditQueryChange: (query) => void loadAuditPage(1, snapshot.auditPageSize ?? 20, query),
    });
  }
  if (!root.contains(securityCenterView.element)) root.replaceChildren(securityCenterView.element);
  return securityCenterView;
}

function render(): void {
  snapshot = {
    ...snapshot,
    workspaceId: workspaceId(),
    mode: currentSecurityMode(),
  };
  ensureSecurityCenter()?.update(snapshot);
}

function bodyOf<T>(result: GatewayResult | undefined, label: string): T {
  if (!result?.ok) throw new Error(`${label}不可用`);
  return (result.body ?? {}) as T;
}

/** Loads each security surface independently so one failed endpoint does not hide the others. */
async function refresh(): Promise<void> {
  snapshot = { ...snapshot, loading: true, error: '', workspaceId: workspaceId() };
  render();
  const auditQuery = snapshot.auditQuery ?? EMPTY_SECURITY_AUDIT_QUERY;
  const auditPageSize = snapshot.auditPageSize ?? 20;
  let fallbackAuditPage = 0;
  const results = await Promise.allSettled([
    window.Crew?.getStrictSecurityEnabled?.(),
    window.Crew?.securityCapabilities?.(),
    window.Crew?.securityRules?.({ workspaceId: workspaceId() }),
    window.Crew?.securityAudit?.({
      limit: auditPageSize,
      offset: ((snapshot.auditPage ?? 1) - 1) * auditPageSize,
      actionType: auditQuery.actionType,
      decision: auditQuery.decision,
      sessionId: auditQuery.sessionId,
      sort: auditQuery.sort,
    }),
  ]);
  const errors: string[] = [];

  const preferenceResult = results[0];
  if (preferenceResult.status === 'fulfilled') {
    const value = preferenceResult.value as { strictSecurityEnabled?: unknown } | undefined;
    snapshot.strictSecurityEnabled = value?.strictSecurityEnabled === true;
  } else {
    errors.push(`全局安全策略：${String(preferenceResult.reason)}`);
  }

  const capabilityResult = results[1];
  if (capabilityResult.status === 'fulfilled') {
    try {
      snapshot.capabilities = bodyOf<SecurityCapabilities>(
        capabilityResult.value as GatewayResult | undefined,
        '防护能力',
      );
    } catch (error) {
      errors.push(String((error as Error).message || error));
    }
  } else {
    errors.push(`防护能力：${String(capabilityResult.reason)}`);
  }

  const rulesResult = results[2];
  if (rulesResult.status === 'fulfilled') {
    try {
      const body = bodyOf<{ rules?: SecurityRuleView[] }>(
        rulesResult.value as GatewayResult | undefined,
        '授权规则',
      );
      snapshot.rules = body.rules ?? [];
    } catch (error) {
      errors.push(String((error as Error).message || error));
    }
  } else {
    errors.push(`授权规则：${String(rulesResult.reason)}`);
  }

  const auditResult = results[3];
  if (auditResult.status === 'fulfilled') {
    try {
      const body = bodyOf<{ events?: SecurityAuditView[]; total?: number }>(
        auditResult.value as GatewayResult | undefined,
        '安全审计',
      );
      snapshot.audits = body.events ?? [];
      snapshot.auditTotal = body.total ?? snapshot.audits.length;
      const lastPage = Math.max(1, Math.ceil(snapshot.auditTotal / auditPageSize));
      if ((snapshot.auditPage ?? 1) > lastPage) fallbackAuditPage = lastPage;
    } catch (error) {
      errors.push(String((error as Error).message || error));
    }
  } else {
    errors.push(`安全审计：${String(auditResult.reason)}`);
  }

  snapshot = { ...snapshot, loading: false, error: errors.join('；') };
  render();
  if (fallbackAuditPage) await loadAuditPage(fallbackAuditPage, auditPageSize, auditQuery);
}

async function changeStrictSecurity(enabled: boolean): Promise<void> {
  const result = await window.Crew?.setStrictSecurityEnabled?.(enabled);
  snapshot = {
    ...snapshot,
    strictSecurityEnabled: result?.strictSecurityEnabled === true,
  };
  render();
  notify('安全策略已保存，网关正在重启以应用新设置。');
}

async function changeMode(mode: ConversationSecurityMode): Promise<void> {
  const accepted = await selectNextConversationMode(
    mode,
    () => showConfirmDialog({
      title: '启用完全访问权限？',
      message: FULL_ACCESS_CONFIRMATION,
      confirmText: '仅对此对话完全放行',
    }),
  );
  if (!accepted) return;
  snapshot = { ...snapshot, mode: currentSecurityMode() };
  render();
}

async function setupProtection(action: 'repair' | 'uninstall'): Promise<void> {
  if (snapshot.setupAction || snapshot.loading) return;
  snapshot = {
    ...snapshot,
    setupAction: action === 'repair' ? 'install' : 'uninstall',
  };
  render();
  try {
    if (action === 'repair') {
      const uacPreparation = await prepareWindowsSecuritySetup();
      if (uacPreparation !== 'ready') return;
    }
    const accepted = await showConfirmDialog({
      title: action === 'repair' ? '安装安全防护' : '卸载安全防护',
      message: action === 'repair'
        ? '将请求一次系统管理员权限，创建受限执行环境并配置网络防护规则。安装完成后，受管命令将在安全边界内执行。是否继续？'
        : '将移除应用创建的安全执行环境和网络防护规则。项目文件不会被删除，但受管命令将暂时无法使用。是否继续？',
      confirmText: action === 'repair' ? '安装并继续' : '卸载并继续',
    });
    if (!accepted) return;
    const result = await window.Crew?.securitySetup?.({ action });
    if (result?.code === 'uac_restart_required') {
      await prepareWindowsSecuritySetup();
      return;
    }
    if (result?.code === 'uac_disabled') {
      await enableUacAndPromptRestart();
      return;
    }
    if (!result?.ok) {
      const detail = result?.detail ? `：${result.detail}` : '';
      notify(action === 'repair'
        ? `安全设置未完成${detail}；托管执行仍保持不可用。`
        : `安全组件卸载未完成${detail}，请使用安装包修复后重试。`);
    }
    await refresh();
  } catch (error) {
    notify(`安全设置未完成：${String(error)}`);
  } finally {
    snapshot = { ...snapshot, setupAction: null };
    render();
  }
}

async function toggleRule(rule: SecurityRuleView): Promise<void> {
  await window.Crew?.securitySetRule?.({
    workspaceId: workspaceId(),
    ruleId: String(rule.rule_id ?? ''),
    enabled: rule.enabled === false,
  });
  await refresh();
}

async function deleteRule(rule: SecurityRuleView): Promise<void> {
  if (!window.confirm('永久删除这条授权规则？此操作不可撤销。')) return;
  await window.Crew?.securityDeleteRule?.({
    workspaceId: workspaceId(),
    ruleId: String(rule.rule_id ?? ''),
  });
  await refresh();
}

async function exportAudit(): Promise<void> {
  const result = await window.Crew?.securityAuditExport?.() as GatewayResult | undefined;
  if (!result?.ok) {
    notify('安全审计导出失败');
    return;
  }
  const jsonl = String((result.body as { jsonl?: string } | undefined)?.jsonl ?? '');
  const url = URL.createObjectURL(new Blob([jsonl], { type: 'application/x-ndjson' }));
  const link = document.createElement('a');
  link.href = url;
  link.download = 'crew-security-audit.jsonl';
  link.click();
  URL.revokeObjectURL(url);
}

async function loadAuditPage(
  page: number,
  pageSize: number,
  query: SecurityAuditQuery = snapshot.auditQuery ?? EMPTY_SECURITY_AUDIT_QUERY,
): Promise<void> {
  const safeSize = Math.max(1, Math.min(100, pageSize));
  const safePage = Math.max(1, page);
  const result = await window.Crew?.securityAudit?.({
    limit: safeSize,
    offset: (safePage - 1) * safeSize,
    actionType: query.actionType,
    decision: query.decision,
    sessionId: query.sessionId,
    sort: query.sort,
  }) as GatewayResult | undefined;
  if (!result?.ok) {
    notify('安全审计加载失败');
    return;
  }
  const body = result.body as {
    events?: SecurityAuditView[];
    total?: number;
  } | undefined;
  const events = body?.events ?? [];
  const total = body?.total ?? events.length;
  const lastPage = Math.max(1, Math.ceil(total / safeSize));
  if (safePage > lastPage) {
    await loadAuditPage(lastPage, safeSize, query);
    return;
  }
  snapshot = {
    ...snapshot,
    audits: events,
    auditPage: safePage,
    auditPageSize: safeSize,
    auditTotal: total,
    auditQuery: { ...query },
  };
  render();
}

async function purgeAudit(): Promise<void> {
  if (!window.confirm('删除超过 30 天的安全审计记录？')) return;
  await window.Crew?.securityAuditPurge?.({ workspaceId: workspaceId() });
  snapshot.auditPage = 1;
  await refresh();
}

export function renderSecurityPage(): void {
  render();
}

export function activateSecurityPage(): void {
  render();
  void refresh();
}

export async function initSecurityPage(): Promise<void> {
  render();
  await refresh();
}

export function __resetSecurityCenterForTest(): void {
  securityCenterView = null;
  snapshot = emptySnapshot();
}
