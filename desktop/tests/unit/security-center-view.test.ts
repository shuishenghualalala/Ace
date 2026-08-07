// @vitest-environment happy-dom

import { beforeEach, describe, expect, it, vi } from 'vitest';

const stateMock = vi.hoisted(() => ({
  currentWorkspaceId: 'workspace-a',
  activeSessionId: null as string | null,
  sessions: [] as Array<{ id: string; workspaceId: string }>,
}));
const showConfirmDialogMock = vi.hoisted(() => vi.fn(async () => true));

vi.mock('../../src/ui/state', () => ({
  $: (selector: string) => document.querySelector(selector),
  escapeHtml: (value: unknown) => String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;'),
  notify: vi.fn(),
  state: stateMock,
}));
vi.mock('../../src/ui/ui-feedback', () => ({
  showConfirmDialog: showConfirmDialogMock,
}));

import { createSecurityCenterView } from '../../src/ui/features/security-center-view';
import {
  __resetSecurityCenterForTest,
  initSecurityPage,
  renderSecurityPage,
} from '../../src/ui/features/security-center';

const capabilities = {
  platform: 'win32',
  helper_present: true,
  filesystem_sandbox: true,
  managed_network: true,
  detail: '安全运行组件已就绪',
};

beforeEach(() => {
  showConfirmDialogMock.mockClear();
  showConfirmDialogMock.mockResolvedValue(true);
  document.body.innerHTML = '<div id="security-page-root"></div>';
  __resetSecurityCenterForTest();
});

describe('Security Center view', () => {
  it('can render inside the audit page without a second page header', () => {
    const view = createSecurityCenterView({
      onRefresh: vi.fn(),
      onStrictSecurityChange: vi.fn(),
      onModeChange: vi.fn(),
      onInstall: vi.fn(),
      onUninstall: vi.fn(),
      onRuleToggle: vi.fn(),
      onRuleDelete: vi.fn(),
      onAuditExport: vi.fn(),
      onAuditPurge: vi.fn(),
    }, { embedded: true });

    expect(view.element.classList.contains('security-center--embedded')).toBe(true);
    expect(view.element.querySelector('.page-header')).toBeNull();
  });

  it('explains mode consequences and renders exact rule scope as text', () => {
    const onModeChange = vi.fn();
    const view = createSecurityCenterView({
      onRefresh: vi.fn(),
      onStrictSecurityChange: vi.fn(),
      onModeChange,
      onInstall: vi.fn(),
      onUninstall: vi.fn(),
      onRuleToggle: vi.fn(),
      onRuleDelete: vi.fn(),
      onAuditExport: vi.fn(),
      onAuditPurge: vi.fn(),
    });
    view.update({
      loading: false,
      error: '',
      workspaceId: 'workspace-a',
      strictSecurityEnabled: true,
      mode: 'request_approval',
      capabilities,
      rules: [{
        rule_id: 'rule-1',
        decision: 'allow',
        kind: 'file',
        cwd: 'D:/work/<img src=x onerror=alert(1)>',
        action_summary: '读取文件：D:/work/<img src=x onerror=alert(1)>',
        action_detail: '文件：D:/work/<img src=x onerror=alert(1)>\n操作：读取文件',
        enabled: true,
      }],
      audits: [],
    });

    expect(view.element.textContent).toContain('每条命令都问我');
    expect(view.element.textContent).toContain('宽权限受管');
    expect(view.element.textContent).toContain('D:/work/<img src=x onerror=alert(1)>');
    expect(view.element.querySelector('.security-center__rule-detail')?.textContent)
      .toContain('操作：读取文件');
    expect(view.element.querySelector('img')).toBeNull();
    expect(view.element.querySelector<HTMLElement>('.security-center__load-state')?.hidden)
      .toBe(true);
    expect(view.element.textContent).not.toContain('安全状态已更新');

    view.element.querySelector<HTMLButtonElement>('[data-security-mode="auto_review"]')?.click();
    expect(onModeChange).toHaveBeenCalledWith('auto_review');
  });

  it('shows built-in Seatbelt status without Windows setup actions on macOS', () => {
    const view = createSecurityCenterView({
      onRefresh: vi.fn(),
      onStrictSecurityChange: vi.fn(),
      onModeChange: vi.fn(),
      onInstall: vi.fn(),
      onUninstall: vi.fn(),
      onRuleToggle: vi.fn(),
      onRuleDelete: vi.fn(),
      onAuditExport: vi.fn(),
      onAuditPurge: vi.fn(),
    });
    view.update({
      loading: false,
      error: '',
      workspaceId: 'workspace-a',
      strictSecurityEnabled: true,
      mode: 'request_approval',
      capabilities: { ...capabilities, platform: 'darwin' },
      rules: [],
      audits: [],
    });

    expect(view.element.querySelector('[data-security-action="install"]')).toBeNull();
    expect(view.element.querySelector('[data-security-action="uninstall"]')).toBeNull();
    expect(view.element.textContent).toContain('系统内置原生防护');
    expect(view.element.textContent).toContain('无需手动安装或申请管理员权限');
    expect(view.element.textContent).not.toMatch(/Windows|Linux|macOS/);
  });

  it('explains a missing macOS runtime instead of claiming built-in protection is ready', () => {
    const view = createSecurityCenterView({
      onRefresh: vi.fn(),
      onStrictSecurityChange: vi.fn(),
      onModeChange: vi.fn(),
      onInstall: vi.fn(),
      onUninstall: vi.fn(),
      onRuleToggle: vi.fn(),
      onRuleDelete: vi.fn(),
      onAuditExport: vi.fn(),
      onAuditPurge: vi.fn(),
    });
    view.update({
      loading: false,
      error: '',
      workspaceId: 'workspace-a',
      strictSecurityEnabled: true,
      mode: 'request_approval',
      capabilities: { platform: 'darwin', helper_present: false },
      rules: [],
      audits: [],
    });

    expect(view.element.textContent).toContain('未找到 macOS 原生安全运行组件');
    expect(view.element.textContent).not.toContain('系统内置原生防护，运行组件随应用提供');
  });

  it('does not treat an unavailable capability response as a missing macOS runtime', () => {
    Object.assign(window, { Crew: { runtimePlatform: 'darwin' } });
    const view = createSecurityCenterView({
      onRefresh: vi.fn(),
      onStrictSecurityChange: vi.fn(),
      onModeChange: vi.fn(),
      onInstall: vi.fn(),
      onUninstall: vi.fn(),
      onRuleToggle: vi.fn(),
      onRuleDelete: vi.fn(),
      onAuditExport: vi.fn(),
      onAuditPurge: vi.fn(),
    });
    view.update({
      loading: false,
      error: '安全能力接口暂时不可用',
      workspaceId: 'workspace-a',
      strictSecurityEnabled: true,
      mode: 'request_approval',
      capabilities: null,
      rules: [],
      audits: [],
    });

    expect(view.element.textContent).toContain('安全能力检测未返回，请点击刷新重试');
    expect(view.element.textContent).not.toContain('未找到 macOS 原生安全运行组件');
  });

  it('keeps only the applicable Windows setup action enabled', () => {
    const view = createSecurityCenterView({
      onRefresh: vi.fn(),
      onStrictSecurityChange: vi.fn(),
      onModeChange: vi.fn(),
      onInstall: vi.fn(),
      onUninstall: vi.fn(),
      onRuleToggle: vi.fn(),
      onRuleDelete: vi.fn(),
      onAuditExport: vi.fn(),
      onAuditPurge: vi.fn(),
    });
    view.update({
      loading: false,
      error: '',
      workspaceId: 'workspace-a',
      strictSecurityEnabled: true,
      mode: 'request_approval',
      capabilities: { ...capabilities, platform: 'windows', filesystem_sandbox: false },
      rules: [],
      audits: [],
    });

    expect(view.element.querySelector<HTMLButtonElement>('[data-security-action="install"]')?.disabled)
      .toBe(false);
    expect(view.element.querySelector<HTMLButtonElement>('[data-security-action="uninstall"]')?.disabled)
      .toBe(true);
    expect(view.element.textContent).not.toContain('当前设备尚未提供原生防护');

    view.update({
      loading: false,
      error: '',
      workspaceId: 'workspace-a',
      strictSecurityEnabled: true,
      mode: 'request_approval',
      capabilities,
      rules: [],
      audits: [],
    });

    expect(view.element.querySelector<HTMLButtonElement>('[data-security-action="install"]')?.disabled)
      .toBe(true);
    expect(view.element.querySelector<HTMLButtonElement>('[data-security-action="uninstall"]')?.disabled)
      .toBe(false);
  });

  it('locks both Windows setup actions while one operation is in progress', () => {
    const view = createSecurityCenterView({
      onRefresh: vi.fn(),
      onStrictSecurityChange: vi.fn(),
      onModeChange: vi.fn(),
      onInstall: vi.fn(),
      onUninstall: vi.fn(),
      onRuleToggle: vi.fn(),
      onRuleDelete: vi.fn(),
      onAuditExport: vi.fn(),
      onAuditPurge: vi.fn(),
    });
    view.update({
      loading: false,
      setupAction: 'install',
      error: '',
      workspaceId: 'workspace-a',
      strictSecurityEnabled: true,
      mode: 'request_approval',
      capabilities: { ...capabilities, filesystem_sandbox: false, managed_network: false },
      rules: [],
      audits: [],
    });

    expect(view.element.querySelector<HTMLButtonElement>('[data-security-action="install"]')?.disabled)
      .toBe(true);
    expect(view.element.querySelector<HTMLButtonElement>('[data-security-action="uninstall"]')?.disabled)
      .toBe(true);
    expect(view.element.textContent).toContain('正在安装…');
  });

  it('hides the strict security toggle while Crew auth allows plaintext HTTP', () => {
    const onStrictSecurityChange = vi.fn();
    const view = createSecurityCenterView({
      onRefresh: vi.fn(),
      onStrictSecurityChange,
      onModeChange: vi.fn(),
      onInstall: vi.fn(),
      onUninstall: vi.fn(),
      onRuleToggle: vi.fn(),
      onRuleDelete: vi.fn(),
      onAuditExport: vi.fn(),
      onAuditPurge: vi.fn(),
    });
    view.update({
      loading: false,
      error: '',
      workspaceId: 'workspace-a',
      strictSecurityEnabled: true,
      mode: 'request_approval',
      capabilities,
      rules: [],
      audits: [],
    });

    // ponytail: 严格安全约束开关暂时隐藏——后端 _ALLOW_INSECURE_AUTH_HTTP 已单独放行
    // Crew 认证 HTTP（见 crew/tools/crew_auth.py），无需用户切全局兼容模式。
    // 见 security-center-view.ts 的 STRICT_SECURITY_TOGGLE_VISIBLE；上游认证服务切 HTTPS 后
    // 改回 true，此处应恢复为「toggle 渲染 + checked 反映 snapshot + change 触发回调」。
    const toggle = view.element.querySelector('[data-security-strict-toggle]');
    expect(toggle).toBeNull();
  });

  it('filters and sorts audit rows, then opens a redacted detail dialog', () => {
    const onAuditQueryChange = vi.fn();
    const view = createSecurityCenterView({
      onRefresh: vi.fn(),
      onStrictSecurityChange: vi.fn(),
      onModeChange: vi.fn(),
      onInstall: vi.fn(),
      onUninstall: vi.fn(),
      onRuleToggle: vi.fn(),
      onRuleDelete: vi.fn(),
      onAuditExport: vi.fn(),
      onAuditPurge: vi.fn(),
      onAuditQueryChange,
    });
    view.update({
      loading: false,
      error: '',
      workspaceId: 'workspace-a',
      strictSecurityEnabled: true,
      mode: 'request_approval',
      capabilities,
      rules: [],
      audits: [{
        event_id: 'event-1',
        timestamp: 1_700_000_000,
        action_type: 'approval_decision',
        decision: 'session',
        session_id: 'session-1234567890',
        session_title: '修复登录问题',
        workspace_id: 'workspace-a',
        workspace_name: '桌面端项目',
        workspace_root: 'D:/work/ace',
        action_summary: '执行命令：git status',
        action_detail: '具体命令：git status --token sk-...3456\n工作目录：D:/work/ace',
        approval_mode: 'auto_review',
        current_approval_mode: 'request_approval',
        decision_source: 'desktop_user',
        tool_name: 'terminal',
      }],
      auditQuery: {
        actionType: '',
        decision: '',
        sessionId: '',
        sort: 'newest',
      },
    });

    expect(view.element.textContent).toContain('session-1234567890');
    expect(view.element.textContent).toContain('执行命令：git status');
    expect(view.element.textContent).toContain('共 1 条');

    const actionType = view.element.querySelector<HTMLSelectElement>('[data-security-audit-filter="action-type"]');
    actionType!.value = 'approval_decision';
    actionType!.dispatchEvent(new Event('change', { bubbles: true }));
    expect(onAuditQueryChange).toHaveBeenCalledWith(expect.objectContaining({
      actionType: 'approval_decision',
    }));

    const sort = view.element.querySelector<HTMLSelectElement>('[data-security-audit-filter="sort"]');
    sort!.value = 'oldest';
    sort!.dispatchEvent(new Event('change', { bubbles: true }));
    expect(onAuditQueryChange).toHaveBeenCalledWith(expect.objectContaining({ sort: 'oldest' }));

    view.element.querySelector<HTMLButtonElement>('[data-security-audit-event="event-1"]')?.click();
    const dialog = document.querySelector('[data-security-audit-dialog]');
    expect(dialog?.textContent).toContain('修复登录问题');
    expect(dialog?.textContent).toContain('桌面端项目');
    expect(dialog?.textContent).toContain('当前会话');
    expect(dialog?.textContent).toContain('事件审批模式替我审批');
    expect(dialog?.textContent).toContain('当前审批模式请求批准');
    expect(dialog?.textContent).toContain('git status --token sk-...3456');
  });
});

describe('Security Center integration', () => {
  it('uses the in-app confirmation and locks setup actions during installation', async () => {
    let finishSetup: ((value: { ok: boolean; exitCode: number }) => void) | undefined;
    const securitySetup = vi.fn(() => new Promise<{ ok: boolean; exitCode: number }>((resolve) => {
      finishSetup = resolve;
    }));
    const incompleteCapabilities = { ...capabilities, filesystem_sandbox: false, managed_network: false };
    Object.assign(window, {
      Crew: {
        getStrictSecurityEnabled: vi.fn(async () => ({ strictSecurityEnabled: true })),
        securityCapabilities: vi.fn(async () => ({ ok: true, body: incompleteCapabilities })),
        securityRules: vi.fn(async () => ({ ok: true, body: { rules: [] } })),
        securityAudit: vi.fn(async () => ({ ok: true, body: { total: 0, events: [] } })),
        securitySetup,
      },
    });

    renderSecurityPage();
    await initSecurityPage();
    document.querySelector<HTMLButtonElement>('[data-security-action="install"]')?.click();

    await vi.waitFor(() => expect(showConfirmDialogMock).toHaveBeenCalledWith(expect.objectContaining({
      title: '安装安全防护',
      confirmText: '安装并继续',
    })));
    expect(document.querySelector<HTMLButtonElement>('[data-security-action="install"]')?.disabled)
      .toBe(true);
    expect(document.querySelector<HTMLButtonElement>('[data-security-action="uninstall"]')?.disabled)
      .toBe(true);
    finishSetup?.({ ok: true, exitCode: 0 });
    await vi.waitFor(() => expect(securitySetup).toHaveBeenCalledWith({ action: 'repair' }));
  });

  it('keeps one shell and loads capabilities, rules and audit through the existing bridge', async () => {
    Object.assign(window, {
      Crew: {
        getStrictSecurityEnabled: vi.fn(async () => ({ strictSecurityEnabled: true })),
        securityCapabilities: vi.fn(async () => ({ ok: true, body: capabilities })),
        securityRules: vi.fn(async () => ({
          ok: true,
          body: {
            rules: [{
              rule_id: 'rule-1',
              decision: 'allow',
              argv_prefix: ['git', 'status'],
              cwd: 'D:/work',
              enabled: true,
            }],
          },
        })),
        securityAudit: vi.fn(async () => ({
          ok: true,
          body: {
            total: 1,
            events: [{
              timestamp: 1_700_000_000,
              action_type: 'exec',
              decision: 'allow',
              sandbox_backend: 'windows',
            }],
          },
        })),
      },
    });

    renderSecurityPage();
    const shell = document.querySelector('[data-security-center]');
    await initSecurityPage();

    expect(document.querySelector('[data-security-center]')).toBe(shell);
    expect(document.body.textContent).toContain('安全运行组件已就绪');
    expect(document.body.textContent).toContain('git status');
    expect(document.body.textContent).toContain('exec');
  });

  it('falls back to the last audit page after records are removed', async () => {
    const securityAudit = vi.fn()
      .mockResolvedValueOnce({ ok: true, body: { total: 21, events: [{}] } })
      .mockResolvedValueOnce({ ok: true, body: { total: 20, events: [] } })
      .mockResolvedValueOnce({
        ok: true,
        body: { total: 20, events: [{ action_summary: '最后一页记录' }] },
      });
    Object.assign(window, {
      Crew: {
        getStrictSecurityEnabled: vi.fn(async () => ({ strictSecurityEnabled: true })),
        securityCapabilities: vi.fn(async () => ({ ok: true, body: capabilities })),
        securityRules: vi.fn(async () => ({ ok: true, body: { rules: [] } })),
        securityAudit,
      },
    });

    renderSecurityPage();
    await initSecurityPage();
    document.querySelector<HTMLButtonElement>('[data-security-audit-page="next"]')?.click();
    await vi.waitFor(() => expect(securityAudit).toHaveBeenCalledTimes(2));
    await vi.waitFor(() => expect(securityAudit).toHaveBeenCalledTimes(3));

    expect(securityAudit.mock.calls[2]?.[0]).toEqual(expect.objectContaining({ offset: 0 }));
    expect(document.body.textContent).toContain('最后一页记录');
  });
});
