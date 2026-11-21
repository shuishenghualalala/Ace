/**
 * @vitest-environment happy-dom
 *
 * security-center 单测：页面生命周期（激活/停用/可用性门控/晚到响应守卫）。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
  __resetSecurityCenterForTest,
  createSecurityPageContribution,
  initSecurityPage,
  refreshSecurityAudit,
} from '../../src/ui/features/security-center';
import { __resetAllStoresForTest } from '../../src/ui/stores/stores';
import { showConfirmDialog } from '../../src/ui/ui-feedback';
import type { SecurityAuditView } from '../../src/ui/features/security-audit';
import type { SecurityCapabilities } from '../../src/ui/features/security-mode';
import type { SecurityRuleView } from '../../src/ui/features/security-rules';

const stateMock = vi.hoisted(() => ({
  currentWorkspaceId: 'workspace-a',
  config: {
    security: { enabled: true },
  },
}));

const notifyMock = vi.hoisted(() => vi.fn());

vi.mock('../../src/ui/state', () => ({
  $: (selector: string) => document.querySelector(selector),
  escapeHtml: (value: unknown) => String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;'),
  notify: notifyMock,
  state: stateMock,
}));

vi.mock('../../src/ui/ui-feedback', () => ({
  showConfirmDialog: vi.fn(async () => true),
}));

const CrewMock = {
  securityCapabilities: vi.fn(async (): Promise<{ ok: boolean; body: SecurityCapabilities }> => ({
    ok: true,
    body: { platform: 'macos', helper_present: true },
  })),
  securityRules: vi.fn(async (_args: unknown) => ({ ok: true, body: { rules: [] as SecurityRuleView[] } })),
  securitySetRule: vi.fn(async (_args: unknown) => ({ ok: true, body: { changed: true } })),
  securityDeleteRule: vi.fn(async (_args: unknown) => ({ ok: true, body: { changed: true } })),
  securityAudit: vi.fn(async (_args: unknown) => ({ ok: true, body: { events: [] as SecurityAuditView[], total: 0 } })),
  securityAuditPurge: vi.fn(async (_args: unknown) => ({ ok: true, body: { deleted: 2 } })),
  securitySetup: vi.fn(async (_args: unknown) => ({ ok: true, exitCode: 0 })),
};

const flush = () => new Promise((r) => setTimeout(r, 0));

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => { resolve = r; });
  return { promise, resolve };
}

function click(selector: string): void {
  const button = document.querySelector<HTMLButtonElement>(selector);
  expect(button).not.toBeNull();
  const disclosure = button?.closest('details');
  if (disclosure) disclosure.open = true;
  button!.click();
}

function changeFilter(filter: string, value: string): void {
  const select = document.querySelector<HTMLSelectElement>(`[data-security-audit-filter="${filter}"]`);
  expect(select).not.toBeNull();
  select!.value = value;
  select!.dispatchEvent(new Event('change', { bubbles: true }));
}

beforeEach(() => {
  vi.clearAllMocks();
  for (const mock of Object.values(CrewMock)) mock.mockReset();
  __resetAllStoresForTest();
  __resetSecurityCenterForTest();
  stateMock.currentWorkspaceId = 'workspace-a';
  stateMock.config = { security: { enabled: true } };
  (window as unknown as { Crew: typeof CrewMock }).Crew = CrewMock;
  document.body.innerHTML = `
    <button class="nav-item" data-tab="security">Security</button>
    <section id="security-tab" class="tab-pane"><div id="security-page-root"></div></section>
  `;
});

describe('Security 页面生命周期', () => {
  it('activate 渲染安全中心并触发刷新', async () => {
    const contribution = createSecurityPageContribution();
    contribution.activate({ signal: new AbortController().signal });
    await flush();

    const root = document.querySelector('#security-page-root');
    expect(root).not.toBeNull();
    expect(CrewMock.securityCapabilities).toHaveBeenCalled();
    expect(CrewMock.securityRules).toHaveBeenCalled();
    expect(CrewMock.securityAudit).toHaveBeenCalled();
  });

  it('deactivate 释放视图并清空根节点', async () => {
    const contribution = createSecurityPageContribution();
    contribution.activate({ signal: new AbortController().signal });
    await flush();

    await contribution.deactivate();

    expect(document.querySelector('#security-page-root')?.children.length).toBe(0);
  });

  it('isAvailable 跟随 securityModuleEnabled', () => {
    const contribution = createSecurityPageContribution();
    expect(contribution.isAvailable?.()).toBe(true);

    stateMock.config = { security: { enabled: false } };
    expect(contribution.isAvailable?.()).toBe(false);
  });

  it('离页后旧刷新响应不回写页面', async () => {
    let resolveCapabilities!: (value: unknown) => void;
    CrewMock.securityCapabilities.mockReturnValueOnce(new Promise((resolve) => { resolveCapabilities = resolve; }));

    const contribution = createSecurityPageContribution();
    const controller = new AbortController();
    contribution.activate({ signal: controller.signal });
    await flush();

    controller.abort();
    await contribution.deactivate();
    resolveCapabilities({
      ok: true,
      body: {
        platform: 'macos',
        helper_present: false,
        filesystem_sandbox: false,
        managed_network: false,
        detail: 'late',
      },
    });
    await flush();

    expect(document.querySelector('#security-page-root')?.textContent).not.toContain('late');
  });
});

describe('Security center requests', () => {
  it('sends account repair through the explicit native repair action', async () => {
    CrewMock.securityCapabilities.mockResolvedValueOnce({
      ok: true,
      body: {
        platform: 'win32',
        helper_present: true,
        filesystem_sandbox: false,
        managed_network: false,
        status: 'unavailable',
        setup_required: false,
        reason: 'windows_sandbox_account_repair_required',
        detail: 'Windows 沙箱账号禁用或密码过期',
      },
    });
    await initSecurityPage();
    click('[data-security-action="repair"]');
    await flush();

    expect(CrewMock.securitySetup).toHaveBeenCalledExactlyOnceWith({ action: 'repair' });
  });

  it('keeps the combined filters and ignores slower audit and whole-page refresh results', async () => {
    type AuditResponse = Awaited<ReturnType<typeof CrewMock.securityAudit>>;
    const oldRefresh = deferred<AuditResponse>();
    const earlierFilter = deferred<AuditResponse>();
    const latestFilter = deferred<AuditResponse>();
    await initSecurityPage();
    CrewMock.securityAudit
      .mockReturnValueOnce(oldRefresh.promise)
      .mockReturnValueOnce(earlierFilter.promise)
      .mockReturnValueOnce(latestFilter.promise);

    const refreshing = initSecurityPage();
    changeFilter('action-type', 'approval_decision');
    changeFilter('sort', 'oldest');
    expect(CrewMock.securityAudit).toHaveBeenLastCalledWith(expect.objectContaining({
      actionType: 'approval_decision', sort: 'oldest', offset: 0,
    }));

    latestFilter.resolve({ ok: true, body: { events: [{ action_summary: '最新筛选结果' }], total: 1 } });
    await flush();
    earlierFilter.resolve({ ok: true, body: { events: [{ action_summary: '旧筛选结果' }], total: 1 } });
    oldRefresh.resolve({ ok: true, body: { events: [{ action_summary: '旧刷新结果' }], total: 1 } });
    await refreshing;
    await flush();
    expect(document.body.textContent).toContain('最新筛选结果');
    expect(document.body.textContent).not.toContain('旧筛选结果');
    expect(document.body.textContent).not.toContain('旧刷新结果');
  });

  it('refreshes only the audit list when chat sends an update while the page is active', async () => {
    refreshSecurityAudit();
    expect(CrewMock.securityAudit).not.toHaveBeenCalled();

    const contribution = createSecurityPageContribution();
    contribution.activate({ signal: new AbortController().signal });
    await flush();
    refreshSecurityAudit();
    await flush();

    expect(CrewMock.securityAudit).toHaveBeenCalledTimes(2);
    expect(CrewMock.securityCapabilities).toHaveBeenCalledTimes(1);
    expect(CrewMock.securityRules).toHaveBeenCalledTimes(1);
  });

  it('shows rules while the native protection check is pending', async () => {
    const nativeCheck = deferred<Awaited<ReturnType<typeof CrewMock.securityCapabilities>>>();
    CrewMock.securityCapabilities.mockReturnValueOnce(nativeCheck.promise);
    CrewMock.securityRules.mockResolvedValueOnce({ ok: true, body: { rules: [{ rule_id: 'one', action_summary: '读取项目文件' }] } });
    const initialized = initSecurityPage();
    await flush();
    expect(document.body.textContent).toContain('读取项目文件');
    nativeCheck.resolve({ ok: true, body: { platform: 'macos', helper_present: true } });
    await initialized;
  });

  it('does not claim success when the server did not change a rule', async () => {
    CrewMock.securityRules.mockResolvedValueOnce({ ok: true, body: { rules: [{ rule_id: 'one', enabled: true }] } });
    CrewMock.securitySetRule.mockResolvedValueOnce({ ok: true, body: { changed: false } });
    await initSecurityPage();
    click('[data-security-action="toggle-rule"]');
    await flush();

    expect(CrewMock.securitySetRule).toHaveBeenCalledWith({ workspaceId: 'workspace-a', ruleId: 'one', enabled: false });
    expect(notifyMock).toHaveBeenCalledWith('授权规则禁用未生效，请检查规则状态。');
    expect(notifyMock).not.toHaveBeenCalledWith('授权规则已禁用');
  });

  it('reports a rule deletion bridge error and keeps the current rule', async () => {
    CrewMock.securityRules.mockResolvedValueOnce({ ok: true, body: { rules: [{ rule_id: 'one' }] } });
    CrewMock.securityDeleteRule.mockRejectedValueOnce(new Error('bridge unavailable'));
    vi.stubGlobal('confirm', () => true);
    try {
      await initSecurityPage();
      click('[data-security-action="rule-menu"]');
      click('[data-menu-item="delete-rule"]');
      await flush();

      expect(notifyMock).toHaveBeenCalledWith('授权规则删除失败');
      expect(document.querySelector('[data-rule-id="one"]')).not.toBeNull();
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it.each([
    { ok: true, deleted: 2, feedback: '已清理 2 条审计记录', expectedLoads: 2 },
    { ok: false, deleted: 0, feedback: '安全审计清理失败', expectedLoads: 1 },
  ])('confirms account-wide retention and reports the result ($feedback)', async ({ ok, deleted, feedback, expectedLoads }) => {
    CrewMock.securityAuditPurge.mockResolvedValueOnce({ ok, body: { deleted } });
    await initSecurityPage();
    click('[data-security-action="audit-menu"]');
    click('[data-menu-item="purge"]');
    await flush();

    expect(showConfirmDialog).toHaveBeenCalledWith(expect.objectContaining({
      message: expect.stringContaining('当前账户在所有工作空间中超过 30 天'),
    }));
    expect(notifyMock).toHaveBeenCalledWith(feedback);
    expect(CrewMock.securityAudit).toHaveBeenCalledTimes(expectedLoads);
  });
});

describe('Windows protection recovery', () => {
  const recovering = {
    ok: true,
    body: { platform: 'windows', status: 'recovering', reason: 'windows_acl_recovery', helper_present: true },
  } as const;

  it('keeps recovery visible until a fresh probe reports readiness', async () => {
    vi.useFakeTimers();
    try {
      const probe = deferred<Awaited<ReturnType<typeof CrewMock.securityCapabilities>>>();
      CrewMock.securityCapabilities.mockResolvedValueOnce(recovering).mockReturnValueOnce(probe.promise);
      const contribution = createSecurityPageContribution();
      contribution.activate({ signal: new AbortController().signal });
      await vi.advanceTimersByTimeAsync(0);
      expect(document.body.textContent).toContain('正在恢复 Windows 沙箱');
      expect(document.querySelector('[data-security-action="install"]')).toBeNull();

      await vi.advanceTimersByTimeAsync(2_000);
      expect(CrewMock.securityCapabilities).toHaveBeenCalledTimes(2);
      expect(document.body.textContent).toContain('正在恢复 Windows 沙箱');
      probe.resolve({ ok: true, body: { platform: 'windows', status: 'ready' } });
      await vi.advanceTimersByTimeAsync(0);
      expect(document.body.textContent).toContain('本机防护已就绪');
      await vi.advanceTimersByTimeAsync(10_000);
      expect(CrewMock.securityCapabilities).toHaveBeenCalledTimes(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it('stops recovery polling when the page is deactivated', async () => {
    vi.useFakeTimers();
    try {
      CrewMock.securityCapabilities.mockResolvedValueOnce(recovering);
      const contribution = createSecurityPageContribution();
      const lifecycle = new AbortController();
      contribution.activate({ signal: lifecycle.signal });
      await vi.advanceTimersByTimeAsync(0);
      expect(document.body.textContent).toContain('正在恢复 Windows 沙箱');

      lifecycle.abort();
      await contribution.deactivate();
      await vi.advanceTimersByTimeAsync(130_000);
      expect(CrewMock.securityCapabilities).toHaveBeenCalledOnce();
      expect(document.querySelector('#security-page-root')?.childElementCount).toBe(0);
    } finally {
      vi.useRealTimers();
    }
  });

  it('offers a fresh check after the recovery window expires', async () => {
    vi.useFakeTimers();
    try {
      CrewMock.securityCapabilities.mockResolvedValue(recovering);
      createSecurityPageContribution().activate({ signal: new AbortController().signal });
      await vi.advanceTimersByTimeAsync(125_000);
      expect(document.body.textContent).toContain('Windows 沙箱恢复耗时较长');
      expect(document.querySelector('[data-security-action="refresh"]')).not.toBeNull();
      expect(document.querySelector('[data-security-action="install"]')).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });

  it('leaves a hung capabilities IPC request behind and ignores its late result after retry', async () => {
    vi.useFakeTimers();
    try {
      const hung = deferred<Awaited<ReturnType<typeof CrewMock.securityCapabilities>>>();
      CrewMock.securityCapabilities.mockReturnValueOnce(hung.promise).mockResolvedValueOnce({
        ok: true,
        body: { platform: 'windows', status: 'ready' },
      });
      const initial = initSecurityPage();
      expect(document.body.textContent).toContain('正在检测本机防护');
      await vi.advanceTimersByTimeAsync(16_000);
      await initial;
      expect(document.body.textContent).toContain('本机防护检测超时');
      click('[data-security-action="refresh"]');
      await vi.advanceTimersByTimeAsync(0);
      hung.resolve(recovering);
      await vi.advanceTimersByTimeAsync(0);
      expect(document.body.textContent).toContain('本机防护已就绪');
      expect(document.body.textContent).not.toContain('正在恢复 Windows 沙箱');
    } finally {
      vi.useRealTimers();
    }
  });
});
