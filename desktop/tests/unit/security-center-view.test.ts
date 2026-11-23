// @vitest-environment happy-dom

import { beforeEach, describe, expect, it, vi } from 'vitest';

const stateMock = vi.hoisted(() => ({
  currentWorkspaceId: 'workspace-a',
  activeSessionId: null as string | null,
  sessions: [] as Array<{ id: string; workspaceId: string }>,
}));
const showConfirmDialogMock = vi.hoisted(() => vi.fn(async () => true));
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
  showConfirmDialog: showConfirmDialogMock,
}));

import { createSecurityCenterView } from '../../src/ui/features/security-center-view';
import type { SecurityCenterActions } from '../../src/ui/features/security-center-view';
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
const setupRequiredCapabilities = {
  ...capabilities,
  filesystem_sandbox: false,
  managed_network: false,
  status: 'setup_required' as const,
  setup_required: true,
};

function makeHandlers(overrides: Partial<SecurityCenterActions> = {}): SecurityCenterActions {
  return {
    onRefresh: vi.fn(),
    onInstall: vi.fn(),
    onRuleToggle: vi.fn(),
    onRuleDelete: vi.fn(),
    onAuditExport: vi.fn(),
    onAuditPurge: vi.fn(),
    ...overrides,
  };
}

beforeEach(() => {
  showConfirmDialogMock.mockClear();
  showConfirmDialogMock.mockResolvedValue(true);
  document.body.innerHTML = '<div id="security-page-root"></div>';
  __resetSecurityCenterForTest();
});

describe('Security Center view', () => {
  it('can render inside the audit page without a second page header', () => {
    const view = createSecurityCenterView(makeHandlers(), { embedded: true });

    expect(view.element.classList.contains('security-center--embedded')).toBe(true);
    expect(view.element.querySelector('.page-header')).toBeNull();
  });

  it('renders exact rule scope and moves deletion into the rule menu', () => {
    const onRuleDelete = vi.fn();
    const view = createSecurityCenterView(makeHandlers({ onRuleDelete }));
    document.body.append(view.element);
    view.update({
      loading: false,
      error: '',
      workspaceId: 'workspace-a',
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

    expect(view.element.textContent).toContain('D:/work/<img src=x onerror=alert(1)>');
    expect(view.element.querySelector('.security-center__rule-detail')?.textContent)
      .toContain('操作：读取文件');
    expect(view.element.querySelector('img')).toBeNull();
    expect(view.element.querySelector<HTMLElement>('.security-center__load-state')?.hidden)
      .toBe(true);
    expect(view.element.textContent).not.toContain('安全状态已更新');
    expect(view.element.querySelector('[data-security-mode]')).toBeNull();
    expect(view.element.querySelector('[data-security-action="refresh"]')).toBeNull();
    expect(view.element.querySelector<HTMLDetailsElement>('[data-security-disclosure="rules"]')?.open).toBe(false);

    view.element.querySelector<HTMLDetailsElement>('[data-security-disclosure="rules"]')!.open = true;
    const more = view.element.querySelector<HTMLButtonElement>('[data-security-action="rule-menu"]')!;
    expect(document.querySelector('[data-menu-item="delete-rule"]')).toBeNull();
    more.click();
    const remove = document.querySelector<HTMLButtonElement>('[data-menu-item="delete-rule"]')!;
    expect(remove.textContent).toBe('永久删除授权规则');
    remove.click();
    expect(onRuleDelete).toHaveBeenCalledWith(expect.objectContaining({ rule_id: 'rule-1' }));
    expect(more.getAttribute('aria-expanded')).toBe('false');
    view.dispose();
  });

  it('explains how to save a rule when the current workspace has none', () => {
    const view = createSecurityCenterView(makeHandlers());
    view.update({ loading: false, error: '', workspaceId: 'workspace-a', capabilities, rules: [], audits: [] });

    const rules = view.element.querySelector('[data-security-disclosure="rules"]');
    expect(rules?.querySelector('summary')?.textContent).toBe('已保存的授权规则');
    expect(rules?.textContent).toContain('此工作空间还没有已保存的规则');
    expect(rules?.textContent).toContain('选择「始终允许此操作」并确认');
    expect(rules?.textContent).toContain('仅在当前账号和工作空间内生效');
  });

  it('offers a retry in the protection card only when the state is unavailable', () => {
    const onRefresh = vi.fn();
    const view = createSecurityCenterView(makeHandlers({ onRefresh }));
    const snapshot = {
      loading: false, error: '', workspaceId: 'workspace-a', capabilities: null, rules: [], audits: [],
    };
    view.update({ ...snapshot, loading: true });
    expect(view.element.querySelector('[data-security-action="refresh"]')).toBeNull();
    view.update(snapshot);
    const retry = view.element.querySelector<HTMLButtonElement>('.security-center__protection [data-security-action="refresh"]');
    expect(retry?.textContent).toBe('重新检测');
    retry?.click();
    expect(onRefresh).toHaveBeenCalledOnce();
    view.update({ ...snapshot, capabilities });
    expect(view.element.querySelector('[data-security-action="refresh"]')).toBeNull();
    view.update({ ...snapshot, capabilities, error: '授权规则加载失败' });
    expect(view.element.querySelector('[data-security-action="refresh"]')?.textContent).toBe('重试加载');
  });

  it.each([
    ['windows_sandbox_busy', 'Windows 沙箱正忙'],
    ['windows_acl_recovery_failed', 'Windows 沙箱恢复未完成'],
  ])('offers recheck without elevation when %s', (reason, label) => {
    const view = createSecurityCenterView(makeHandlers());
    view.update({
      loading: false, error: '', workspaceId: 'workspace-a', rules: [], audits: [],
      capabilities: { ...setupRequiredCapabilities, reason, status: 'unavailable' },
    });
    expect(view.element.textContent).toContain(label);
    expect(view.element.querySelector('[data-security-action="install"]')).toBeNull();
    expect(view.element.querySelector('[data-security-action="refresh"]')?.textContent).toBe('重新检测');
  });

  it('offers explicit administrator repair for unhealthy sandbox accounts', () => {
    const view = createSecurityCenterView(makeHandlers());
    view.update({
      loading: false, error: '', workspaceId: 'workspace-a', rules: [], audits: [],
      capabilities: {
        ...setupRequiredCapabilities,
        status: 'unavailable',
        setup_required: false,
        reason: 'windows_sandbox_account_repair_required',
        detail: 'Windows 沙箱账号缺失、禁用或密码过期。请由管理员检查账号后重新检测。',
      },
    });
    expect(view.element.textContent).toContain('Windows 沙箱账号需要管理员修复');
    expect(view.element.textContent).toContain('请由管理员检查账号后重新检测');
    expect(view.element.querySelector('[data-security-action="install"]')).toBeNull();
    const repair = view.element.querySelector<HTMLButtonElement>('[data-security-action="repair"]');
    expect(repair?.textContent).toBe('管理员修复 Windows 沙箱');
    expect(view.element.querySelector('[data-security-action="refresh"]')?.textContent).toBe('重新检测');
    const onInstall = vi.fn();
    const interactive = createSecurityCenterView(makeHandlers({ onInstall }));
    interactive.update({
      loading: false, error: '', workspaceId: 'workspace-a', rules: [], audits: [],
      capabilities: {
        ...setupRequiredCapabilities,
        status: 'unavailable',
        setup_required: false,
        reason: 'windows_sandbox_account_repair_required',
      },
    });
    interactive.element.querySelector<HTMLButtonElement>('[data-security-action="repair"]')?.click();
    expect(onInstall).toHaveBeenCalledOnce();
  });

  it.each([['darwin', 'macOS'], ['linux', 'Linux']])('reports automatic %s protection without an install action', (platform, label) => {
    const view = createSecurityCenterView(makeHandlers());
    view.update({
      loading: false,
      error: '',
      workspaceId: 'workspace-a',
      capabilities: { ...capabilities, platform },
      rules: [],
      audits: [],
    });

    expect(view.element.querySelector<HTMLButtonElement>('[data-security-action="install"]'))
      .toBeNull();
    expect(view.element.textContent).toContain(`${label} 防护已可用于 Ace 的受管命令`);
  });

  it('shows one Windows setup action only after an explicit setup result', () => {
    const view = createSecurityCenterView(makeHandlers());
    view.update({
      loading: false,
      error: '',
      workspaceId: 'workspace-a',
      capabilities: setupRequiredCapabilities,
      rules: [],
      audits: [],
    });

    expect(view.element.querySelector<HTMLButtonElement>('[data-security-action="install"]')?.disabled)
      .toBe(false);
    expect(view.element.querySelector('[data-security-action="install"]')?.textContent).toBe('设置 Windows 防护');
    expect(view.element.querySelector('[data-security-action="uninstall"]')).toBeNull();
    expect(view.element.textContent).not.toContain('当前设备尚未提供原生防护');

    view.update({
      loading: false,
      error: '',
      workspaceId: 'workspace-a',
      capabilities: { ...capabilities, status: 'ready' },
      rules: [],
      audits: [],
    });

    expect(view.element.querySelector('[data-security-action="install"]')).toBeNull();
    expect(view.element.querySelector('[data-security-action="uninstall"]')).toBeNull();
    expect(view.element.textContent).toContain('本机防护已就绪');
  });

  it.each([
    null,
    { ...capabilities, helper_present: false, status: 'unavailable' as const },
    { ...setupRequiredCapabilities, runtime_stale: true },
    { ...capabilities, workspace_network_access: true, direct_network: false },
    { ...setupRequiredCapabilities, platform: 'linux' },
  ])('does not offer elevated setup for an inconclusive or non-actionable detection: %j', (reported) => {
    const view = createSecurityCenterView(makeHandlers());
    view.update({
      loading: false,
      error: '',
      workspaceId: 'workspace-a',
      capabilities: reported,
      rules: [],
      audits: [],
    });

    expect(view.element.querySelector('[data-security-action="install"]')).toBeNull();
  });

  it('shows the gateway detail when the native runtime is unavailable', () => {
    const view = createSecurityCenterView(makeHandlers());
    view.update({
      loading: false,
      error: '',
      workspaceId: 'workspace-a',
      capabilities: {
        ...capabilities,
        helper_present: false,
        status: 'unavailable',
        detail: '未找到本机防护运行组件。开发态请先构建并 staging 原生运行组件。',
      },
      rules: [],
      audits: [],
    });

    expect(view.element.textContent).toContain('开发态请先构建并 staging 原生运行组件。');
  });

  it('locks the Windows setup action while the operation is in progress', () => {
    const view = createSecurityCenterView(makeHandlers());
    view.update({
      loading: false,
      settingUp: true,
      error: '',
      workspaceId: 'workspace-a',
      capabilities: setupRequiredCapabilities,
      rules: [],
      audits: [],
    });

    expect(view.element.querySelector<HTMLButtonElement>('[data-security-action="install"]')?.disabled)
      .toBe(true);
    expect(view.element.textContent).toContain('正在设置…');
  });

  it('filters and sorts audit rows, then opens a redacted detail dialog', () => {
    const onAuditQueryChange = vi.fn();
    const view = createSecurityCenterView(makeHandlers({ onAuditQueryChange }));
    view.update({
      loading: false,
      error: '',
      workspaceId: 'workspace-a',
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
    expect(Array.from(actionType!.options).map((option) => option.value)).toEqual(expect.arrayContaining([
      'approval_requested',
      'approval_decision',
      'exec_decision',
      'file_decision',
    ]));
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
    expect(dialog?.querySelectorAll('button[aria-label="关闭"]')).toHaveLength(1);
    expect(dialog?.querySelector('.mw-dialog__footer')).toBeNull();
    view.dispose();
  });

  it('hides single-page navigation and names the account-wide audit actions in their menu', () => {
    const onAuditExport = vi.fn();
    const onAuditPurge = vi.fn();
    const view = createSecurityCenterView(makeHandlers({ onAuditExport, onAuditPurge }));
    document.body.append(view.element);
    view.update({ loading: false, error: '', workspaceId: 'workspace-a', capabilities, rules: [], audits: [], auditTotal: 20 });
    expect(view.element.querySelector('[data-pager-id="security-audit"]')).toBeNull();
    view.element.querySelector<HTMLDetailsElement>('[data-security-disclosure="audit"]')!.open = true;
    const more = view.element.querySelector<HTMLButtonElement>('[data-security-action="audit-menu"]')!;
    more.click();
    const exportItem = document.querySelector<HTMLButtonElement>('[data-menu-item="export"]')!;
    expect(exportItem.textContent).toBe('导出账号全部审计记录');
    exportItem.click();
    expect(onAuditExport).toHaveBeenCalledOnce();
    more.click();
    expect(document.querySelector('[data-menu-item="purge"]')?.textContent).toBe('清理账号内 30 天前的审计记录');
    view.dispose();
    expect(document.querySelector('[data-menu-item="purge"]')).toBeNull();
    expect(onAuditPurge).not.toHaveBeenCalled();
  });

  it('keeps an in-progress audit filter while the protection card refreshes', () => {
    const view = createSecurityCenterView(makeHandlers());
    const snapshot = {
      loading: false, error: '', workspaceId: 'workspace-a', capabilities, rules: [], audits: [],
    };
    view.update(snapshot);
    const input = view.element.querySelector<HTMLInputElement>('[data-security-audit-session]')!;
    input.value = 'session-in-progress';
    view.update({ ...snapshot, loading: true });
    expect(view.element.querySelector('[data-security-audit-session]')).toBe(input);
    expect(input.value).toBe('session-in-progress');
  });
});

describe('Security Center integration', () => {
  it('starts Windows setup from one explicit action and locks it during installation', async () => {
    let finishSetup: ((value: { ok: boolean; exitCode: number }) => void) | undefined;
    const securitySetup = vi.fn(() => new Promise<{ ok: boolean; exitCode: number }>((resolve) => {
      finishSetup = resolve;
    }));
    const securityCapabilities = vi.fn()
      .mockResolvedValueOnce({ ok: true, body: setupRequiredCapabilities })
      .mockResolvedValueOnce({ ok: true, body: capabilities });
    Object.assign(window, {
      Crew: {
        securityCapabilities,
        securityRules: vi.fn(async () => ({ ok: true, body: { rules: [] } })),
        securityAudit: vi.fn(async () => ({ ok: true, body: { total: 0, events: [] } })),
        securitySetup,
      },
    });

    renderSecurityPage();
    await initSecurityPage();
    expect(securitySetup).not.toHaveBeenCalled();
    document.querySelector<HTMLButtonElement>('[data-security-action="install"]')?.click();

    await vi.waitFor(() => expect(securitySetup).toHaveBeenCalledExactlyOnceWith({ action: 'install' }));
    expect(showConfirmDialogMock).not.toHaveBeenCalled();
    expect(document.querySelector<HTMLButtonElement>('[data-security-action="install"]')?.disabled)
      .toBe(true);
    finishSetup?.({ ok: true, exitCode: 0 });
    await vi.waitFor(() => expect(document.querySelector('[data-security-action="install"]')).toBeNull());
    expect(document.body.textContent).toContain('本机防护已就绪');
  });

  it('explains when Windows needs UAC for setup without changing system settings', async () => {
    const securityUacStatus = vi.fn();
    const securityEnableUac = vi.fn();
    const securitySetup = vi.fn(async () => ({ ok: false, exitCode: null, code: 'uac_disabled' }));
    Object.assign(window, {
      Crew: {
        securityCapabilities: vi.fn(async () => ({ ok: true, body: setupRequiredCapabilities })),
        securityRules: vi.fn(async () => ({ ok: true, body: { rules: [] } })),
        securityAudit: vi.fn(async () => ({ ok: true, body: { total: 0, events: [] } })),
        securityUacStatus,
        securityEnableUac,
        securitySetup,
      },
    });

    await initSecurityPage();
    const currentInstall = () => document.querySelector<HTMLButtonElement>('[data-security-action="install"]');
    currentInstall()?.click();
    await vi.waitFor(() => expect(notifyMock).toHaveBeenCalledWith('请先在 Windows 中开启用户账户控制（UAC），然后再试。'));
    expect(currentInstall()?.disabled).toBe(false);
    expect(securitySetup).toHaveBeenCalledExactlyOnceWith({ action: 'install' });
    expect(securityUacStatus).not.toHaveBeenCalled();
    expect(securityEnableUac).not.toHaveBeenCalled();
    expect(showConfirmDialogMock).not.toHaveBeenCalled();
  });

  it('keeps one shell and loads capabilities, rules and audit through the existing bridge', async () => {
    Object.assign(window, {
      Crew: {
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
    expect(document.body.textContent).toContain('本机防护已就绪');
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
        securityCapabilities: vi.fn(async () => ({ ok: true, body: capabilities })),
        securityRules: vi.fn(async () => ({ ok: true, body: { rules: [] } })),
        securityAudit,
      },
    });

    renderSecurityPage();
    await initSecurityPage();
    document.querySelector<HTMLDetailsElement>('[data-security-disclosure="audit"]')!.open = true;
    document.querySelector<HTMLButtonElement>('[data-security-audit-page="next"]')?.click();
    await vi.waitFor(() => expect(securityAudit).toHaveBeenCalledTimes(3));

    expect(securityAudit.mock.calls[1]?.[0]).toEqual(expect.objectContaining({ offset: 20 }));
    expect(securityAudit.mock.calls[2]?.[0]).toEqual(expect.objectContaining({ offset: 0 }));
    expect(document.body.textContent).toContain('最后一页记录');
    expect(document.querySelector<HTMLDetailsElement>('[data-security-disclosure="audit"]')?.open).toBe(true);
  });
});
