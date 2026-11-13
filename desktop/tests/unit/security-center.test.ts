/**
 * @vitest-environment happy-dom
 *
 * security-center 单测：页面生命周期（激活/停用/可用性门控/晚到响应守卫）。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
  __resetSecurityCenterForTest,
  createSecurityPageContribution,
} from '../../src/ui/features/security-center';
import { __resetAllStoresForTest, configStore } from '../../src/ui/stores/stores';

const stateMock = vi.hoisted(() => ({
  currentWorkspaceId: 'workspace-a',
  activeSessionId: null as string | null,
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
  getStrictSecurityEnabled: vi.fn(async () => ({ strictSecurityEnabled: true })),
  securityCapabilities: vi.fn(async () => ({ ok: true, body: { platform: 'macos', helper_present: true } })),
  securityRules: vi.fn(async () => ({ ok: true, body: { rules: [] } })),
  securityAudit: vi.fn(async () => ({ ok: true, body: { events: [], total: 0 } })),
};

const flush = () => new Promise((r) => setTimeout(r, 0));

beforeEach(() => {
  vi.clearAllMocks();
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
    CrewMock.securityCapabilities.mockReturnValue(new Promise((resolve) => { resolveCapabilities = resolve; }));

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
