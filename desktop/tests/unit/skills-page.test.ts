/**
 * @vitest-environment happy-dom
 *
 * skills-page 单测：页面生命周期（激活/停用/资源释放/晚到响应守卫）。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { backendApi } from '../../src/ui/backend-client';
import { __resetAllStoresForTest } from '../../src/ui/stores/stores';

const mockShowConfirmDialog = vi.hoisted(() => vi.fn(async () => true));

vi.mock('../../src/ui/backend-client', () => ({
  backendApi: {
    skillStore: vi.fn(),
    plugins: vi.fn(),
  },
}));

vi.mock('../../src/ui/ui-feedback', () => ({
  showConfirmDialog: mockShowConfirmDialog,
  showPromptDialog: vi.fn(async () => null),
}));

const api = backendApi as unknown as {
  skillStore: ReturnType<typeof vi.fn>;
  plugins: ReturnType<typeof vi.fn>;
};

const flush = () => new Promise((r) => setTimeout(r, 0));

beforeEach(() => {
  vi.clearAllMocks();
  mockShowConfirmDialog.mockResolvedValue(true);
  __resetAllStoresForTest();
  document.body.innerHTML = `
    <button class="nav-item" data-tab="skills">Skills</button>
    <section id="skills-tab" class="tab-pane"><div id="skills-page-root"></div></section>
  `;
  api.skillStore.mockResolvedValue({
    ok: true,
    installed: [],
    optional: [],
    evolution: { auto_trigger: false, auto_full_cycle: false, visible: false },
  });
  api.plugins.mockResolvedValue([]);
});

describe('Skills 页面生命周期', () => {
  it('activate 渲染技能页面并触发加载', async () => {
    const { createSkillsPageContribution } = await import('../../src/ui/features/skills-page');
    const contribution = createSkillsPageContribution();

    contribution.activate({ signal: new AbortController().signal });
    await flush();

    const root = document.querySelector('#skills-page-root');
    expect(root).not.toBeNull();
    expect(api.skillStore).toHaveBeenCalled();
    expect(api.plugins).toHaveBeenCalled();
  });

  it('deactivate 释放 capabilityHubView 并清空根节点', async () => {
    const { createSkillsPageContribution } = await import('../../src/ui/features/skills-page');
    const contribution = createSkillsPageContribution();

    contribution.activate({ signal: new AbortController().signal });
    await flush();
    await contribution.deactivate();

    expect(document.querySelector('#skills-page-root')?.children.length).toBe(0);
  });

  it('离页后旧加载响应不回写页面', async () => {
    let resolveStore!: (value: unknown) => void;
    api.skillStore.mockReturnValue(new Promise((resolve) => { resolveStore = resolve; }));
    const { createSkillsPageContribution } = await import('../../src/ui/features/skills-page');
    const contribution = createSkillsPageContribution();
    const controller = new AbortController();

    contribution.activate({ signal: controller.signal });
    await flush();
    controller.abort();
    await contribution.deactivate();
    resolveStore({
      ok: true,
      installed: [{ slug: 'late', name: 'Late Skill', source: 'builtin' }],
      optional: [],
    });
    await flush();

    expect(document.querySelector('#skills-page-root')?.textContent).not.toContain('Late Skill');
  });
});
