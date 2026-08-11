/**
 * @vitest-environment happy-dom
 */
import { describe, expect, it } from 'vitest';
import { resolveShellNavigation } from '../../src/ui/features/sidebar-nav';
import { createApplicationShell } from '../../src/ui/layouts/application-shell';

describe('resolveShellNavigation', () => {
  it('assistant mode keeps Skills, exposes Inspiration/Security and removes Audit', () => {
    const navigation = resolveShellNavigation('assistant', { agents: 'available' });
    const ids = navigation.map((item) => item.id);

    expect(ids).toContain('skills');
    expect(navigation).toContainEqual(expect.objectContaining({
      id: 'sites',
      label: '灵感',
      featureState: 'available',
    }));
    expect(ids).toContain('security');
    expect(ids).not.toContain('audit');
  });

  it('places the Crew brand above the centered horizontal navigation rail', () => {
    localStorage.clear();
    const shell = createApplicationShell({
      features: { agents: 'available' },
      storage: localStorage,
    });
    document.body.replaceChildren(shell.element);

    const navigation = shell.element.querySelector('.mw-app-navigation');
    const brand = navigation?.firstElementChild;
    const chat = navigation?.querySelector<HTMLElement>('[data-shell-location="chat"]');

    expect(brand?.classList.contains('mw-sidebar-brand')).toBe(true);
    expect(brand?.textContent).toBe('Crew');
    expect(shell.element.querySelector('.mw-app-titlebar .mw-sidebar-brand')).toBeNull();
    expect(chat?.children[0]?.classList.contains('mw-shell-nav-item__icon')).toBe(true);
    expect(chat?.children[1]?.textContent).toBe('对话');

    shell.dispose();
  });
});
