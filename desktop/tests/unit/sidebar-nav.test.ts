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

  it('marks the security item unavailable when the security module is off', () => {
    const navigation = resolveShellNavigation('assistant', { security: 'unavailable' });
    expect(navigation).toContainEqual(expect.objectContaining({
      id: 'security',
      featureState: 'unavailable',
    }));
  });

  it('disables the security nav item and shows the developing hint when off', () => {
    localStorage.clear();
    const shell = createApplicationShell({
      features: { security: 'unavailable' },
      storage: localStorage,
    });
    document.body.replaceChildren(shell.element);

    const security = shell.element.querySelector<HTMLButtonElement>('[data-shell-location="security"]');
    expect(security?.disabled).toBe(true);
    expect(security?.title).toBe('功能正在开发中，敬请期待');
    expect(security?.querySelector('.mw-shell-nav-item__availability')).toBeNull();

    shell.dispose();
  });
});
