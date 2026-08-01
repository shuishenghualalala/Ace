import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

const stylesDir = resolve(process.cwd(), 'assets/styles');
const layoutCss = readFileSync(resolve(stylesDir, 'layout.css'), 'utf8');
const chatCss = readFileSync(resolve(stylesDir, 'chat.css'), 'utf8');

function ruleBody(css: string, selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const match = css.match(new RegExp(`(?:^|\\n)\\s*${escaped}\\s*\\{([^}]*)\\}`));
  expect(match, `missing CSS rule: ${selector}`).not.toBeNull();
  return match?.[1] ?? '';
}

describe('shared chat chrome styles', () => {
  it('keeps the chat sub-navigation independent from optional modules', () => {
    expect(ruleBody(layoutCss, '.chat-subnav')).toContain('border-radius: var(--radius-full)');
    expect(ruleBody(layoutCss, 'body.history-workspace-active .chat-subnav')).toContain('display: inline-flex');

    const item = ruleBody(layoutCss, '.chat-subnav-item');
    expect(item).toContain('border: 1px solid transparent');
    expect(item).toContain('font-size: var(--font-sm)');

    const active = ruleBody(layoutCss, '.chat-subnav-item.active');
    expect(active).toContain('background: var(--accent-soft)');
    expect(active).toContain('font-weight: 600');
  });

  it('keeps the chat disclaimer styled as muted centered copy', () => {
    const disclaimer = ruleBody(chatCss, '.chat-disclaimer');
    expect(disclaimer).toContain('width: min(920px, 100%)');
    expect(disclaimer).toContain('font-size: 11px');
    expect(disclaimer).toContain('text-align: center');
    expect(disclaimer).toContain('color: var(--text-muted-1)');
    expect(ruleBody(chatCss, '#chat-tab.active.chat-mode .chat-disclaimer')).toContain('display: block');
  });
});
