/**
 * @vitest-environment happy-dom
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { showFileOpenMenu } from '../../src/ui/features/file-open-menu';

vi.mock('../../src/ui/state', () => ({
  notify: vi.fn(),
}));

const showItemInFolder = vi.fn(async () => undefined);
const listOpenApplications = vi.fn(async () => [
  { id: 'mac:com.kingsoft.wpsoffice.mac', name: 'WPS Office' },
  { id: 'mac:com.apple.Pages', name: 'Pages' },
]);
const openPathWith = vi.fn(async () => ({ ok: true as const }));

beforeEach(() => {
  document.body.innerHTML = '';
  showItemInFolder.mockClear();
  listOpenApplications.mockClear();
  openPathWith.mockClear();
  Object.defineProperty(window, 'Crew', {
    configurable: true,
    value: {
      showItemInFolder,
      listOpenApplications,
      openPathWith,
    },
  });
});

describe('file open menu', () => {
  it('lists reveal and compatible application actions, then launches the selected app', async () => {
    const anchor = document.createElement('button');
    anchor.setAttribute('aria-expanded', 'false');
    document.body.appendChild(anchor);

    await showFileOpenMenu(anchor, '/workspace/report.docx');

    const menu = document.querySelector<HTMLElement>('.file-open-menu');
    expect(menu?.getAttribute('role')).toBe('menu');
    expect(menu?.textContent).toContain('在资源管理器中显示');
    expect(menu?.textContent).toContain('打开于 WPS Office');
    expect(menu?.textContent).toContain('打开于 Pages');
    expect(anchor.getAttribute('aria-expanded')).toBe('true');
    expect(listOpenApplications).toHaveBeenCalledWith('/workspace/report.docx');

    const wps = Array.from(menu?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]') ?? [])
      .find((button) => button.textContent?.includes('WPS Office'));
    wps?.click();
    await Promise.resolve();

    expect(openPathWith).toHaveBeenCalledWith(
      '/workspace/report.docx',
      'mac:com.kingsoft.wpsoffice.mac',
    );
    expect(document.querySelector('.file-open-menu')).toBeNull();
    expect(anchor.getAttribute('aria-expanded')).toBe('false');
  });
});
