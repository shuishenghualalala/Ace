/**
 * Security Center composition root.
 *
 * The renderer view owns presentation only; requests use the preload security APIs.
 */

import { $, notify, state } from '../state';
import { showConfirmDialog } from '../ui-feedback';
import {
  createSecurityCenterView,
  type SecurityCenterSnapshot,
  type SecurityCenterView,
} from './security-center-view';
import type { SecurityCapabilities } from './security-mode';
import type { SecurityRuleView } from './security-rules';
import {
  EMPTY_SECURITY_AUDIT_QUERY,
  type SecurityAuditQuery,
  type SecurityAuditView,
} from './security-audit';
import type { PageContribution, PageLifecycleContext } from './page-registry';
import { securityModuleEnabled, windowsSetupRequired } from './security-mode';

type GatewayResult = { ok?: boolean; body?: unknown };
type SecurityResource = 'capabilities' | 'rules' | 'audit';
type AuditRequest = { page: number; pageSize: number; query: SecurityAuditQuery };

const CAPABILITY_CHECK_TIMEOUT_MS = 16_000;
const RECOVERY_RECHECK_INTERVAL_MS = 2_000;
const RECOVERY_WAIT_MS = 125_000;

class CapabilityCheckTimeout extends Error {}

let securityCenterView: SecurityCenterView | null = null;
let snapshot: SecurityCenterSnapshot = emptySnapshot();
let auditRequest: AuditRequest = initialAuditRequest();
const requestRevisions = { capabilities: 0, rules: 0, audit: 0 };
let loadErrors: Partial<Record<SecurityResource, string>> = {};
const pendingRules = new Set<string>();
let purgingAudit = false;
let capabilityCheck: AbortController | undefined;
let recoveryRecheck: ReturnType<typeof setTimeout> | undefined;

// ── 页面生命周期状态 ──
let securityLifecycleSignal: AbortSignal | null = null;
let securityLifecycleEpoch = 0;

function securityLifecycleAvailable(): boolean {
  return !securityLifecycleSignal || !securityLifecycleSignal.aborted;
}

function activePageGuard(): () => boolean {
  const epoch = securityLifecycleEpoch;
  return () => epoch === securityLifecycleEpoch && securityLifecycleAvailable();
}

function latestRequestGuard(resource: SecurityResource): () => boolean {
  const stillActive = activePageGuard();
  const revision = ++requestRevisions[resource];
  return () => stillActive() && revision === requestRevisions[resource];
}

function workspaceId(): string {
  return state.currentWorkspaceId ?? 'default';
}

function initialAuditRequest(): AuditRequest {
  return { page: 1, pageSize: 20, query: { ...EMPTY_SECURITY_AUDIT_QUERY } };
}

function emptySnapshot(): SecurityCenterSnapshot {
  const { page, pageSize, query } = initialAuditRequest();
  return {
    loading: false,
    error: '',
    workspaceId: workspaceId(),
    capabilities: null,
    rules: [],
    audits: [],
    auditPage: page,
    auditPageSize: pageSize,
    auditTotal: 0,
    auditQuery: query,
  };
}

function ensureSecurityCenter(): SecurityCenterView | null {
  const root = $('#security-page-root');
  if (!root) return null;
  if (!securityCenterView) {
    securityCenterView = createSecurityCenterView({
      onRefresh: () => void refresh(),
      onInstall: () => void setupProtection(),
      onRuleToggle: (rule) => void toggleRule(rule),
      onRuleDelete: (rule) => void deleteRule(rule),
      onAuditExport: () => void exportAudit(),
      onAuditPurge: () => void purgeAudit(),
      onAuditPageChange: (page) => void loadAuditPage({ ...auditRequest, page }, true),
      onAuditPageSizeChange: (pageSize) => void loadAuditPage({ ...auditRequest, page: 1, pageSize }, true),
      onAuditQueryChange: (patch) => void loadAuditPage({
        ...auditRequest,
        page: 1,
        query: { ...auditRequest.query, ...patch },
      }, true),
    }, { embedded: root.closest('#settings-pane-security') !== null });
  }
  if (!root.contains(securityCenterView.element)) root.replaceChildren(securityCenterView.element);
  return securityCenterView;
}

function render(): void {
  const currentWorkspace = workspaceId();
  if (snapshot.workspaceId !== currentWorkspace) {
    snapshot = { ...snapshot, workspaceId: currentWorkspace, rules: [] };
    delete loadErrors.rules;
  }
  snapshot.error = Object.values(loadErrors).join('；');
  ensureSecurityCenter()?.update(snapshot);
}

function bodyOf<T>(result: GatewayResult | undefined, label: string): T {
  if (!result?.ok) throw new Error(label);
  return (result.body ?? {}) as T;
}

/** Loads each security surface independently so one failed endpoint does not hide the others. */
async function refresh(): Promise<void> {
  if (!securityLifecycleAvailable()) return;
  await Promise.all([refreshCapabilities(), refreshRules(), loadAuditPage(auditRequest)]);
}

function cancelCapabilityCheck(): void {
  capabilityCheck?.abort();
  clearTimeout(recoveryRecheck);
  recoveryRecheck = undefined;
}

async function readCapabilities(signal: AbortSignal, timeoutMs: number): Promise<GatewayResult | undefined> {
  let timeout: ReturnType<typeof setTimeout> | undefined;
  let onAbort: () => void = () => {};
  const stop = new Promise<never>((_, reject) => {
    onAbort = () => reject(signal.reason);
    signal.addEventListener('abort', onAbort, { once: true });
    timeout = setTimeout(() => reject(new CapabilityCheckTimeout()), timeoutMs);
  });
  try {
    return await Promise.race([stop, window.Crew?.securityCapabilities?.()]);
  } finally {
    clearTimeout(timeout);
    signal.removeEventListener('abort', onAbort);
  }
}

async function refreshCapabilities(recoveryDeadline?: number): Promise<void> {
  const isCurrent = latestRequestGuard('capabilities');
  cancelCapabilityCheck();
  const check = new AbortController();
  capabilityCheck = check;
  const lifecycle = securityLifecycleSignal;
  const cancelOnLeave = () => check.abort();
  lifecycle?.addEventListener('abort', cancelOnLeave, { once: true });
  if (recoveryDeadline === undefined) {
    snapshot.loading = true;
    delete loadErrors.capabilities;
    render();
  }
  try {
    const remaining = recoveryDeadline === undefined ? CAPABILITY_CHECK_TIMEOUT_MS : recoveryDeadline - Date.now();
    if (remaining <= 0) throw new CapabilityCheckTimeout();
    const result = await readCapabilities(check.signal, Math.min(CAPABILITY_CHECK_TIMEOUT_MS, remaining));
    if (!isCurrent()) return;
    snapshot.capabilities = bodyOf<SecurityCapabilities>(result, '无法读取防护状态');
    if (snapshot.capabilities.status === 'recovering' && lifecycle) {
      const deadline = recoveryDeadline ?? Date.now() + RECOVERY_WAIT_MS;
      recoveryRecheck = setTimeout(() => {
        if (isCurrent()) void refreshCapabilities(deadline);
      }, Math.min(RECOVERY_RECHECK_INTERVAL_MS, Math.max(0, deadline - Date.now())));
    }
  } catch (error) {
    if (!isCurrent()) return;
    snapshot.capabilities = null;
    if (error instanceof CapabilityCheckTimeout) {
      loadErrors.capabilities = recoveryDeadline === undefined
        ? '本机防护检测超时'
        : 'Windows 沙箱恢复耗时较长，请稍后重新检测';
    } else {
      loadErrors.capabilities = '无法读取防护状态';
    }
  } finally {
    lifecycle?.removeEventListener('abort', cancelOnLeave);
    if (capabilityCheck === check) capabilityCheck = undefined;
    if (isCurrent()) {
      snapshot.loading = false;
      render();
    }
  }
}

async function refreshRules(): Promise<void> {
  const targetWorkspace = workspaceId();
  const isCurrent = latestRequestGuard('rules');
  try {
    const result = await window.Crew?.securityRules?.({ workspaceId: targetWorkspace });
    if (!isCurrent() || workspaceId() !== targetWorkspace) return;
    const body = bodyOf<{ rules?: SecurityRuleView[] }>(result, '无法读取授权规则');
    snapshot.rules = body.rules ?? [];
    delete loadErrors.rules;
  } catch {
    if (!isCurrent() || workspaceId() !== targetWorkspace) return;
    loadErrors.rules = '无法读取授权规则';
  }
  render();
}

async function setupProtection(): Promise<void> {
  if (snapshot.settingUp || snapshot.loading || !windowsSetupRequired(snapshot.capabilities)) return;
  const stillActive = activePageGuard();
  snapshot = { ...snapshot, settingUp: true };
  render();
  try {
    const result = await window.Crew?.securitySetup?.({ action: 'install' });
    if (!stillActive()) return;
    if (result?.code === 'uac_restart_required') {
      notify('请重启电脑，使 Windows 的用户账户控制设置生效。');
      return;
    }
    if (result?.code === 'uac_disabled') {
      notify('请先在 Windows 中开启用户账户控制（UAC），然后再试。');
      return;
    }
    if (!result?.ok) {
      const detail = result?.detail ? `：${result.detail}` : '';
      notify(`Windows 防护设置未完成${detail}`);
    }
    await refreshCapabilities();
  } catch (error) {
    if (stillActive()) notify(`Windows 防护设置未完成：${String(error)}`);
  } finally {
    if (stillActive()) {
      snapshot = { ...snapshot, settingUp: false };
      render();
    }
  }
}

async function toggleRule(rule: SecurityRuleView): Promise<void> {
  const enabled = rule.enabled === false;
  await mutateRule(rule, enabled ? '启用' : '禁用', (target) => (
    window.Crew?.securitySetRule?.({ ...target, enabled })
  ));
}

async function deleteRule(rule: SecurityRuleView): Promise<void> {
  if (!window.confirm('永久删除这条授权规则？此操作不可撤销。')) return;
  await mutateRule(rule, '删除', (target) => window.Crew?.securityDeleteRule?.(target));
}

async function mutateRule(
  rule: SecurityRuleView,
  action: string,
  mutate: (target: { workspaceId: string; ruleId: string }) => Promise<GatewayResult> | undefined,
): Promise<void> {
  if (!securityLifecycleAvailable() || snapshot.workspaceId !== workspaceId() || !rule.rule_id) return;
  const target = { workspaceId: snapshot.workspaceId, ruleId: rule.rule_id };
  const key = JSON.stringify([target.workspaceId, target.ruleId]);
  if (pendingRules.has(key)) return;
  pendingRules.add(key);
  const stillActive = activePageGuard();
  const matchesWorkspace = () => stillActive() && target.workspaceId === workspaceId();
  try {
    const result = await mutate(target);
    if (!matchesWorkspace()) return;
    const body = bodyOf<{ changed?: boolean }>(result, `授权规则${action}失败`);
    if (body.changed !== true) {
      notify(`授权规则${action}未生效，请检查规则状态。`);
      await refreshRules();
      return;
    }
    notify(`授权规则已${action}`);
    void loadAuditPage(auditRequest);
    await refreshRules();
  } catch {
    if (matchesWorkspace()) notify(`授权规则${action}失败`);
  } finally {
    if (stillActive()) pendingRules.delete(key);
  }
}

async function exportAudit(): Promise<void> {
  const stillActive = activePageGuard();
  try {
    const result = await window.Crew?.securityAuditExport?.();
    if (!stillActive()) return;
    const { jsonl } = bodyOf<{ jsonl?: string }>(result, '安全审计导出失败');
    if (typeof jsonl !== 'string') throw new Error('安全审计导出失败');
    const url = URL.createObjectURL(new Blob([jsonl], { type: 'application/x-ndjson' }));
    const link = document.createElement('a');
    link.href = url;
    link.download = 'crew-security-audit.jsonl';
    try {
      link.click();
    } finally {
      URL.revokeObjectURL(url);
    }
  } catch {
    if (stillActive()) notify('安全审计导出失败');
  }
}

async function loadAuditPage(request: AuditRequest, reportFailure = false): Promise<void> {
  if (!securityLifecycleAvailable()) return;
  const isCurrent = latestRequestGuard('audit');
  const { query } = request;
  const pageSize = Math.max(1, Math.min(100, Math.trunc(request.pageSize)));
  let page = Math.max(1, Math.trunc(request.page));
  auditRequest = { page, pageSize, query };
  try {
    for (;;) {
      const result = await window.Crew?.securityAudit?.({
        limit: pageSize,
        offset: (page - 1) * pageSize,
        ...query,
      });
      if (!isCurrent()) return;
      const body = bodyOf<{ events?: SecurityAuditView[]; total?: number }>(result, '安全审计加载失败');
      const events = body.events ?? [];
      const total = body.total ?? events.length;
      const lastPage = Math.max(1, Math.ceil(total / pageSize));
      if (page > lastPage) {
        page = lastPage;
        continue;
      }
      auditRequest = { page, pageSize, query };
      snapshot = { ...snapshot, audits: events, auditPage: page, auditPageSize: pageSize, auditTotal: total, auditQuery: query };
      delete loadErrors.audit;
      break;
    }
  } catch {
    if (!isCurrent()) return;
    loadErrors.audit = '安全审计加载失败';
    if (reportFailure) notify(loadErrors.audit);
  }
  render();
}

async function purgeAudit(): Promise<void> {
  if (purgingAudit || !securityLifecycleAvailable()) return;
  purgingAudit = true;
  const stillActive = activePageGuard();
  const targetWorkspace = workspaceId();
  try {
    const accepted = await showConfirmDialog({
      title: '清理旧审计记录？',
      message: '将删除当前账户在所有工作空间中超过 30 天的安全审计记录。使用中的筛选条件不影响清理范围。',
      confirmText: '删除旧记录',
    });
    if (!accepted || !stillActive()) return;
    const result = await window.Crew?.securityAuditPurge?.({ workspaceId: targetWorkspace });
    if (!stillActive()) return;
    const { deleted } = bodyOf<{ deleted: number }>(result, '安全审计清理失败');
    if (!Number.isInteger(deleted) || deleted < 0) throw new Error('安全审计清理失败');
    notify(deleted === 0 ? '没有超过 30 天的审计记录' : `已清理 ${deleted} 条审计记录`);
    void loadAuditPage(auditRequest);
  } catch {
    if (stillActive()) notify('安全审计清理失败');
  } finally {
    if (stillActive()) purgingAudit = false;
  }
}

export function renderSecurityPage(): void {
  render();
}

export function refreshSecurityAudit(): void {
  if (securityLifecycleSignal && securityLifecycleAvailable()) void loadAuditPage(auditRequest);
}

export async function initSecurityPage(): Promise<void> {
  render();
  await refresh();
}

export function __resetSecurityCenterForTest(): void {
  resetPage();
}

function resetPage(): void {
  securityLifecycleEpoch += 1;
  cancelCapabilityCheck();
  securityLifecycleSignal = null;
  securityCenterView?.dispose();
  securityCenterView = null;
  snapshot = emptySnapshot();
  auditRequest = initialAuditRequest();
  loadErrors = {};
  pendingRules.clear();
  purgingAudit = false;
}

export function createSecurityPageContribution(): PageContribution {
  return {
    id: 'security',
    isAvailable: () => securityModuleEnabled(),
    activate: ({ signal }: PageLifecycleContext) => {
      securityLifecycleEpoch += 1;
      securityLifecycleSignal = signal;
      render();
      void refresh();
    },
    deactivate: resetPage,
  };
}
