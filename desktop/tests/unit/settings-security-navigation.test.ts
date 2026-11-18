// @vitest-environment happy-dom

import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { expect, it, vi } from 'vitest';
import { createSettingsShell } from '../../src/ui/features/settings-shell';

it('opens the Security pane with dedicated permission and security-center roots', () => {
  const template = document.createElement('template');
  template.innerHTML = readFileSync(resolve(process.cwd(), 'assets/index.html'), 'utf8');
  const panes = [...template.content.querySelectorAll<HTMLElement>('#settings-pane-staging > .set-v2-pane')];
  expect(template.content.querySelector('#security-tab')).toBeNull();

  const onPaneChange = vi.fn();
  const shell = createSettingsShell({ panes, onPaneChange, onClose: () => shell.close() });
  document.body.append(shell.element);
  try {
    shell.open();
    const security = shell.element.querySelector<HTMLButtonElement>('[data-settings-pane="security"]');
    const pane = shell.element.querySelector<HTMLElement>('#settings-pane-security');
    expect(security?.textContent).toBe('安全');
    expect(pane?.hidden).toBe(true);

    security?.click();
    expect(onPaneChange).toHaveBeenCalledWith('security');
    expect(security?.getAttribute('aria-current')).toBe('page');
    expect(pane?.hidden).toBe(false);
    expect(pane?.querySelector('#settings-security-permissions-root + #security-page-root')).not.toBeNull();

    shell.element.querySelector<HTMLButtonElement>('[data-settings-pane="general"]')?.click();
    expect(onPaneChange).toHaveBeenLastCalledWith('general');
    expect(pane?.hidden).toBe(true);
  } finally {
    shell.dispose();
    shell.element.remove();
  }
});
