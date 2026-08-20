import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

const stylesDir = resolve(process.cwd(), 'assets/styles');
const studioCss = readFileSync(resolve(stylesDir, 'studio.css'), 'utf8');
const layoutCss = readFileSync(resolve(stylesDir, 'layouts.css'), 'utf8');
const chatCss = readFileSync(resolve(stylesDir, 'chat.css'), 'utf8');
const webMessagesCss = readFileSync(resolve(stylesDir, 'web-messages.css'), 'utf8');
const kanbanCss = readFileSync(resolve(stylesDir, 'kanban-board.css'), 'utf8');
const composerCss = readFileSync(resolve(stylesDir, 'composer.css'), 'utf8');
const composerContextCss = readFileSync(resolve(stylesDir, 'composer-context.css'), 'utf8');
const processTimelineCss = readFileSync(resolve(stylesDir, 'process-timeline.css'), 'utf8');
const streamChatCss = readFileSync(resolve(stylesDir, 'stream-chat.css'), 'utf8');
const uiPreviewCss = readFileSync(resolve(stylesDir, 'ui-preview.css'), 'utf8');
const securityCenterCss = readFileSync(resolve(stylesDir, 'security-center.css'), 'utf8');
const shellCss = readFileSync(resolve(stylesDir, 'shell.css'), 'utf8');
const tokensCss = readFileSync(resolve(stylesDir, 'tokens.css'), 'utf8');
const welcomeCss = readFileSync(resolve(stylesDir, 'welcome-scenarios.css'), 'utf8');

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

  it('keeps a narrow-window safety gutter without shifting the desktop message track', () => {
    const messageTrack = ruleBody(webMessagesCss, '.chat-messages.web-flow .messages__inner');
    expect(messageTrack).toContain(
      'width: min(calc(var(--mw-chat-content-max-width) + var(--mw-space-8)), 100%)',
    );
    expect(messageTrack).toContain('padding: 0 var(--mw-space-4)');
  });

  it('keeps assistant turns and user bubbles symmetrically inset', () => {
    const assistantTurn = ruleBody(webMessagesCss, '.chat-messages.web-flow .msg.msg--agent-turn');
    const userTurn = ruleBody(webMessagesCss, '.chat-messages.web-flow .msg.user');
    expect(assistantTurn).toContain('padding-left: var(--mw-space-4)');
    expect(userTurn).toContain('padding-right: var(--mw-space-4)');
  });

  it('keeps the running assistant on the same responsive axis as agent turns', () => {
    const composer = ruleBody(composerCss, '.chat-composer.mw-composer');
    const runningSlot = ruleBody(composerCss, '.mw-composer__running-slot');
    const runningLogo = ruleBody(streamChatCss, '.running-intro__logo');
    const runningImage = ruleBody(streamChatCss, '.running-intro__agent-logo');
    expect(composer).toContain('padding: var(--mw-space-4) var(--mw-space-4) var(--mw-space-3)');
    expect(runningSlot).toContain('padding-inline: var(--mw-space-4)');
    expect(runningLogo).toContain('width: 38px');
    expect(runningLogo).toContain('height: 38px');
    expect(runningImage).toContain('width: 38px');
    expect(runningImage).toContain('height: 38px');
    expect(runningImage).not.toContain('translate(');
    expect(uiPreviewCss).not.toMatch(/(?:^|\n)\s*\.chat-composer\s*\{/);
  });

  it('keeps Request/Response code readable on light process surfaces', () => {
    expect(ruleBody(processTimelineCss, '.mw-process-timeline .process-code-block pre')).toContain(
      'color: var(--mw-text-primary)',
    );
    expect(ruleBody(streamChatCss, '.process-code-block pre')).toContain(
      'color: var(--mw-text-primary)',
    );
  });

  it('keeps selected mention input text from duplicating the overlay copy', () => {
    const source = ruleBody(chatCss, '.chat-input-container textarea.chat-input-overlay-source');
    const selection = ruleBody(
      chatCss,
      '.chat-input-container textarea.chat-input-overlay-source::selection',
    );
    const overlay = ruleBody(composerContextCss, '.chat-input-overlay');

    expect(source).toContain('color: transparent');
    expect(selection).toContain('color: transparent');
    expect(selection).toContain('-webkit-text-fill-color: transparent');
    expect(overlay).toContain('pointer-events: none');
  });

  it('keeps Team member avatars and bubbles visibly colored for the first tones', () => {
    expect(ruleBody(kanbanCss, '.team-collaboration-board .agent-tone-0'))
      .toContain('background: var(--mw-status-success-bg)');
    expect(ruleBody(kanbanCss, '.team-collaboration-board .agent-tone-1'))
      .toContain('background: var(--mw-status-info-bg)');
    expect(ruleBody(webMessagesCss, '.chat-messages.web-flow .agent-avatar--message.agent-tone-0'))
      .toContain('border-color: var(--mw-status-success)');
    expect(ruleBody(webMessagesCss, '.chat-messages.web-flow .agent-avatar--message.agent-tone-1'))
      .toContain('border-color: var(--mw-status-info)');
    expect(ruleBody(webMessagesCss, '.chat-messages.web-flow .team-internal__bubble--tone-0:not(.is-crew)'))
      .toContain('var(--mw-status-success)');
    expect(ruleBody(webMessagesCss, '.chat-messages.web-flow .team-internal__bubble--tone-1:not(.is-crew)'))
      .toContain('var(--mw-status-info)');
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

describe('Crew shell and Welcome identity', () => {
  it('keeps the responsive full-height rail with a compact icon-only mode', () => {
    const tokens = ruleBody(tokensCss, ':root');
    expect(tokens).toContain('--mw-app-rail-width: 96px');
    expect(tokens).toContain('--mw-app-rail-width-compact: 56px');
    const brand = ruleBody(shellCss, '.mw-sidebar-brand');
    expect(brand).toContain('flex-direction: column');
    expect(brand).toContain('min-height: 136px');
    expect(brand).toContain('padding: var(--mw-space-10)');
    expect(ruleBody(shellCss, '.mw-app-navigation')).toContain(
      'grid-template-rows: auto minmax(0, 1fr) auto',
    );
    expect(shellCss).toContain('grid-template-columns: 22px minmax(0, 32px)');
    expect(shellCss).toContain(
      'grid-template-columns: var(--mw-app-rail-width-compact) 240px minmax(0, 1fr)',
    );
    expect(shellCss).toContain('.mw-sidebar-brand__label');
  });

  it('anchors the mascot and larger tiger paws to the Welcome project strip', () => {
    expect(ruleBody(welcomeCss, '.welcome-view__mascot')).toContain('width: 184px');
    expect(ruleBody(welcomeCss, '.welcome-view__title')).toContain('font: 700');
    const paw = ruleBody(welcomeCss, '.mw-composer__welcome-paw');
    expect(paw).toContain('width: 38px');
    expect(paw).toContain('border: 4px solid var(--mw-text-primary)');
    expect(welcomeCss).toContain('.mw-composer__project:not([hidden])');
    expect(ruleBody(welcomeCss, 'body.welcome-active #chat-composer-root')).toContain(
      'position: relative',
    );
    expect(welcomeCss).toContain('@media (prefers-reduced-motion: reduce)');
  });

  it('lifts the mascot clear of the Composer in minimum-height windows', () => {
    const compactHeightStart = welcomeCss.indexOf('@media (max-height: 680px)');
    const reducedMotionStart = welcomeCss.indexOf('@media (prefers-reduced-motion: reduce)');
    const compactHeightCss = welcomeCss.slice(compactHeightStart, reducedMotionStart);

    expect(compactHeightStart).toBeGreaterThanOrEqual(0);
    expect(reducedMotionStart).toBeGreaterThan(compactHeightStart);
    expect(compactHeightCss).toContain(
      'padding: var(--mw-space-2) var(--mw-space-4) var(--mw-space-4)',
    );
    expect(compactHeightCss).toContain('width: 140px');
    expect(compactHeightCss).toContain('height: 140px');
    expect(compactHeightCss).not.toContain('.mw-composer__welcome-paws');
  });

  it('keeps the mention canvas above the Welcome mascot without lifting the project strip', () => {
    expect(welcomeCss).toContain(
      'body.welcome-active #chat-composer-root:has(.mention-pop)',
    );
    expect(welcomeCss).toContain(
      '#chat-composer-root:has(.mention-pop) .mw-composer',
    );
    expect(
      ruleBody(
        welcomeCss,
        'body.welcome-active\n  #chat-composer-root\n  .mw-composer__panel:has(.mention-pop)',
      ),
    ).toContain('z-index: 3');
  });

  it('lets compact Welcome content scroll instead of clipping its text', () => {
    const compactHeightStart = welcomeCss.indexOf('@media (max-height: 680px)');
    const reducedMotionStart = welcomeCss.indexOf('@media (prefers-reduced-motion: reduce)');
    const compactHeightCss = welcomeCss.slice(compactHeightStart, reducedMotionStart);

    expect(compactHeightCss).toContain('overflow-y: auto');
    expect(compactHeightCss).toContain('scroll-padding-block: var(--mw-space-4)');
    expect(compactHeightCss).toContain('grid-row: 1');
    expect(compactHeightCss).toContain('grid-row: auto');
    expect(welcomeCss).toContain(
      'grid-template-rows: minmax(0, 1fr) auto minmax(0, 1fr)',
    );
    expect(compactHeightCss).not.toContain(
      'grid-template-rows: minmax(0, 1fr) auto auto',
    );
    expect(compactHeightCss).not.toContain('translate(-50%, 0) rotate(-8deg)');
    expect(compactHeightCss).not.toContain('translate(50%, 0) rotate(8deg)');
  });
});
