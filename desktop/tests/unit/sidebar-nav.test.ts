/**
 * @vitest-environment happy-dom
 */
import { describe, expect, it } from 'vitest';
import {
  createShellNavigationRegistry,
  resolveShellNavigation,
  ShellNavigationRegistry,
} from '../../src/ui/features/sidebar-nav';
import {
  canNavigateToWiki,
  resolveTabAfterWikiCapabilityChange,
  wikiFeatureEnabled,
} from '../../src/ui/features/wiki-feature';
import type { BackendConfig } from '../../src/ui/backend-client';
import { createApplicationShell } from '../../src/ui/layouts/application-shell';
import { defaultProductModeState, PRODUCT_MODE_STORAGE_KEY, productModeStore } from '../../src/ui/stores/product-mode-store';

describe('resolveShellNavigation', () => {
  it('projects the Wiki contribution only when its capability is available', () => {
    expect(resolveShellNavigation('assistant', { wiki: 'available' }).map((item) => item.id)).toContain('wiki');
    expect(resolveShellNavigation('work', { wiki: 'available' }).map((item) => item.id)).toContain('wiki');
    expect(resolveShellNavigation('assistant', { wiki: 'hidden' }).map((item) => item.id)).not.toContain('wiki');
    expect(resolveShellNavigation('work', { wiki: 'hidden' }).map((item) => item.id)).not.toContain('wiki');
    expect(resolveShellNavigation('assistant', { wiki: 'hidden' }).map((item) => item.id)).toContain('chat');
  });

  it('keeps the workbench availability gate from the original work inventory', () => {
    expect(resolveShellNavigation('work').find((item) => item.id === 'workbench')).toMatchObject({
      featureState: 'unavailable',
    });
    expect(resolveShellNavigation('work', { work: { workbench: 'available' } }))
      .toContainEqual(expect.objectContaining({ id: 'workbench', featureState: 'available' }));
  });

  it('orders contributions deterministically and rejects conflicts', () => {
    const registry = new ShellNavigationRegistry();
    const disposeLate = registry.register({ id: 'chat', label: '对话', icon: 'process-thinking', order: 20 });
    registry.register({ id: 'wiki', label: '笔记', icon: 'icon-wiki', order: 10 });
    expect(registry.resolve('assistant').map((item) => item.id)).toEqual(['wiki', 'chat']);
    expect(() => registry.register({ id: 'chat', label: '重复', icon: 'process-thinking', order: 30 })).toThrow(
      'already registered',
    );
    expect(registry.unregister('chat')).toBe(true);
    expect(registry.unregister('chat')).toBe(false);
    registry.register({ id: 'chat', label: '替换', icon: 'process-thinking', order: 30 });
    disposeLate();
    expect(registry.resolve('assistant').map((item) => item.id)).toEqual(['wiki', 'chat']);
  });

  it('mounts Wiki explicitly in the default host registry', () => {
    const registry = createShellNavigationRegistry();
    expect(registry.resolve('assistant').some((item) => item.id === 'wiki')).toBe(true);
  });

  it('keeps the legacy visible default while config is loading or unavailable', () => {
    expect(wikiFeatureEnabled(null)).toBe(true);
    expect(wikiFeatureEnabled({} as BackendConfig)).toBe(true);
    expect(wikiFeatureEnabled({ wiki: { enabled: false } } as BackendConfig)).toBe(false);
    expect(canNavigateToWiki({ wiki: { enabled: false } } as BackendConfig)).toBe(false);
    expect(canNavigateToWiki(null)).toBe(true);
  });

  it('falls back from an active Wiki tab only when capability becomes disabled', () => {
    expect(resolveTabAfterWikiCapabilityChange('wiki', false)).toBe('chat');
    expect(resolveTabAfterWikiCapabilityChange('wiki', true)).toBe('wiki');
    expect(resolveTabAfterWikiCapabilityChange('chat', false)).toBe('chat');
    expect(resolveTabAfterWikiCapabilityChange('agents', false)).toBe('agents');
  });

  it('keeps Skills and Inspiration while Security and Audit live off the main rail', () => {
    const navigation = resolveShellNavigation('assistant', { agents: 'available' });
    const ids = navigation.map((item) => item.id);

    expect(ids).toContain('skills');
    expect(navigation).toContainEqual(expect.objectContaining({
      id: 'sites',
      label: '灵感',
      featureState: 'available',
    }));
    expect(ids).not.toContain('security');
    expect(ids).not.toContain('audit');
  });

  it('keeps the tracing entry hidden until the dev capability gate is ready', () => {
    expect(resolveShellNavigation('assistant', { tracing: 'hidden' }).map((item) => item.id)).not.toContain('tracing');
    expect(resolveShellNavigation('assistant', { tracing: 'available' })).toContainEqual(expect.objectContaining({
      id: 'tracing',
      featureState: 'available',
    }));
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

  it('restores a saved standalone Security location to the Chat page', () => {
    localStorage.clear();
    const saved = defaultProductModeState();
    saved.views.assistant.lastPosition = 'security';
    localStorage.setItem(PRODUCT_MODE_STORAGE_KEY, JSON.stringify(saved));
    const shell = createApplicationShell({ storage: localStorage });
    document.body.replaceChildren(shell.element);

    expect(productModeStore.get().views.assistant.lastPosition).toBe('chat');
    expect(shell.element.querySelector('[data-shell-location="security"]')).toBeNull();
    expect(shell.element.querySelector('[data-shell-location="chat"]')?.getAttribute('aria-current')).toBe('page');

    shell.dispose();
  });

  it('hides Wiki when its capability is disabled without affecting chat navigation', () => {
    localStorage.clear();
    const navigated: string[] = [];
    const shell = createApplicationShell({
      features: { agents: 'available', wiki: 'hidden' },
      storage: localStorage,
      onNavigate: (location) => {
        navigated.push(location);
        return true;
      },
    });
    document.body.replaceChildren(shell.element);

    expect(shell.element.querySelector('[data-shell-location="wiki"]')).toBeNull();
    const chat = shell.element.querySelector<HTMLButtonElement>('[data-shell-location="chat"]');
    chat?.click();
    expect(navigated).toEqual(['chat']);
    shell.dispose();
  });

  it('makes the assistant history boundary draggable and persistent', () => {
    localStorage.clear();
    const shell = createApplicationShell({
      features: { agents: 'available' },
      storage: localStorage,
    });
    document.body.replaceChildren(shell.element);

    const sash = shell.element.querySelector<HTMLElement>('[data-shell-context-sash]');
    const template = shell.element.querySelector<HTMLElement>('.mw-shell-template');
    expect(sash).not.toBeNull();
    expect(template).not.toBeNull();
    expect(sash?.getAttribute('role')).toBe('separator');
    expect(sash?.hidden).toBe(false);

    sash?.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, clientX: 200 }));
    document.dispatchEvent(new MouseEvent('mousemove', { bubbles: true, clientX: 280 }));
    expect(template?.style.getPropertyValue('--mw-app-context-width')).toBe('340px');
    document.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, clientX: 280 }));
    expect(localStorage.getItem('crew.desktop.appContextWidth.v1')).toBe('340');

    sash?.dispatchEvent(new MouseEvent('dblclick', { bubbles: true }));
    expect(template?.style.getPropertyValue('--mw-app-context-width')).toBe('');
    expect(localStorage.getItem('crew.desktop.appContextWidth.v1')).toBeNull();

    shell.dispose();
  });
});
