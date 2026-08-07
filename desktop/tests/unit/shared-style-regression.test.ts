import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

const stylesDir = resolve(process.cwd(), 'assets/styles');
const studioCss = readFileSync(resolve(stylesDir, 'studio.css'), 'utf8');
const layoutCss = readFileSync(resolve(stylesDir, 'layouts.css'), 'utf8');
const chatCss = readFileSync(resolve(stylesDir, 'chat.css'), 'utf8');
const securityCenterCss = readFileSync(resolve(stylesDir, 'security-center.css'), 'utf8');

function ruleBody(css: string, selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const match = css.match(new RegExp(`(?:^|\\n)\\s*${escaped}\\s*\\{([^}]*)\\}`));
  expect(match, `missing CSS rule: ${selector}`).not.toBeNull();
  return match?.[1] ?? '';
}

describe('shared chat chrome styles', () => {
  it('keeps the Composer toolbar surface transparent', () => {
    expect(ruleBody(chatCss, '.chat-input-toolbar')).toContain('background: transparent');
  });

  it('keeps the chat sub-navigation independent from optional modules', () => {
    expect(ruleBody(studioCss, '.chat-subnav')).toContain('border-radius: var(--mw-radius-full)');
    expect(ruleBody(studioCss, 'body.history-workspace-active .chat-subnav')).toContain('display: inline-flex');

    const item = ruleBody(studioCss, '.chat-subnav-item');
    expect(item).toContain('border: 1px solid transparent');
    expect(item).toContain('font: inherit');

    const active = ruleBody(studioCss, '.chat-subnav-item.active');
    expect(active).toContain('background: var(--mw-bg-selected)');
    expect(active).toContain('font-weight: 600');
  });

  it('keeps the chat disclaimer styled as muted centered copy', () => {
    const disclaimer = ruleBody(layoutCss, '.tab-pane#chat-tab.active.chat-mode .chat-disclaimer');
    expect(disclaimer).toContain('grid-row: 3');
    expect(disclaimer).toContain('font-size: var(--mw-type-caption-size)');
    expect(disclaimer).toContain('text-align: center');
    expect(disclaimer).toContain('color: var(--mw-text-muted)');
  });
});

describe('security center scroll contract', () => {
  it('gives the standalone security page root a constrained flex height', () => {
    const root = ruleBody(securityCenterCss, '.security-page-root');
    expect(root).toContain('display: flex');
    expect(root).toContain('flex: 1 1 auto');
    expect(root).toContain('min-height: 0');
  });
});
