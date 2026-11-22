/** @vitest-environment happy-dom */

import { beforeEach, expect, it, vi } from 'vitest';
import { __resetAllStoresForTest } from '../../src/ui/stores/stores';

const apiMock = vi.hoisted(() => ({
  skillStore: vi.fn(),
  plugins: vi.fn(),
  skillSources: vi.fn(),
  addSkillSource: vi.fn(),
  removeSkillSource: vi.fn(),
  refreshSkillSource: vi.fn(),
  importSkillDirectory: vi.fn(),
}));

vi.mock('../../src/ui/backend-client', () => ({ backendApi: apiMock }));
vi.mock('../../src/ui/ui-feedback', () => ({
  showConfirmDialog: vi.fn(async () => true),
  showPromptDialog: vi.fn(async () => null),
}));

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

beforeEach(() => {
  vi.clearAllMocks();
  __resetAllStoresForTest();
  document.body.innerHTML = `
    <button class="nav-item" data-tab="skills">Skills</button>
    <section id="skills-tab" class="tab-pane"><div id="skills-page-root"></div></section>`;
  apiMock.skillStore.mockResolvedValue({
    ok: true,
    installed: [],
    optional: [],
    evolution: { auto_trigger: false, auto_full_cycle: false, visible: false },
  });
  apiMock.plugins.mockResolvedValue([]);
  apiMock.skillSources.mockResolvedValue({ sources: [] });
  apiMock.addSkillSource.mockResolvedValue({
    ok: true,
    source: { kind: 'dir', path: 'D:\\team-skills', label: 'team-skills' },
  });
});

it('registers a market source from the skills page', async () => {
  const { createSkillsPageContribution } = await import('../../src/ui/features/skills-page');
  const contribution = createSkillsPageContribution();
  contribution.activate({ signal: new AbortController().signal });
  await flush();

  const control = document.querySelector<HTMLButtonElement>('[data-market-source-control]');
  expect(control).not.toBeNull();
  control!.click();
  await vi.waitFor(() => expect(document.querySelector('[role="dialog"]')).not.toBeNull());

  const input = document.querySelector<HTMLInputElement>('input[aria-label="市场源地址"]')!;
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
  setter?.call(input, 'D:\\team-skills');
  input.dispatchEvent(new Event('input', { bubbles: true }));
  apiMock.skillSources.mockResolvedValue({
    sources: [{ kind: 'dir', path: 'D:\\team-skills', label: 'team-skills' }],
  });

  const form = input.closest('form')!;
  form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
  await vi.waitFor(() => expect(apiMock.addSkillSource).toHaveBeenCalledWith('D:\\team-skills'));
  await vi.waitFor(() => expect(document.querySelector('[role="dialog"]')?.textContent).toContain('team-skills'));
  expect(apiMock.skillStore).toHaveBeenCalledTimes(2);
});
