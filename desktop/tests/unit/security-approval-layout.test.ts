import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { describe, expect, it } from 'vitest';

const desktopRoot = resolve(__dirname, '../..');

describe('security approval layout contract', () => {
  it('aligns the running status with the same centered content width as the approval panel', () => {
    const css = readFileSync(resolve(desktopRoot, 'assets/styles/composer.css'), 'utf8');

    expect(css).toMatch(/\.mw-composer__queue-slot,[\s\S]*?\.chat-running-intro,[\s\S]*?width: min\(var\(--mw-chat-content-max-width\), 100%\)/);
  });

  it('keeps long approval details contained within the viewport', () => {
    const css = readFileSync(resolve(desktopRoot, 'assets/styles/web-messages.css'), 'utf8');

    expect(css).toContain('max-height: min(70dvh, 460px)');
    expect(css).toContain('overscroll-behavior: contain');
  });
});
