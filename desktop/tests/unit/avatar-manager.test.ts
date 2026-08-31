/** @vitest-environment happy-dom */
import { describe, expect, it } from 'vitest';
import {
  avatarMarkup,
  avatarTone,
  createAvatarElement,
  parseAvatarRef,
} from '../../src/ui/avatar-manager';

describe('avatar manager', () => {
  it('uses one Crew default avatar for DOM and markup consumers', () => {
    const element = createAvatarElement({ kind: 'crew' });
    expect(element.querySelector('use')?.getAttribute('href')).toBe('./crew-ui-symbols.svg#avatar-headphones');
    expect(avatarMarkup({ kind: 'crew' })).toContain('href="#avatar-headphones"');
  });

  it('renders companion user and agent image references without owning their data', () => {
    const user = createAvatarElement({
      kind: 'companion-user',
      id: 'peer-1',
      name: '小明',
      avatar: { kind: 'image', src: 'data:image/png;base64,abc' },
    });
    const agent = createAvatarElement({
      kind: 'companion-agent',
      id: 'agent-1',
      name: '研究 Agent',
      avatar: { kind: 'image', src: 'https://example.com/agent.png' },
    });

    expect(user.querySelector<HTMLImageElement>('img')?.src).toContain('data:image/png;base64,abc');
    expect(agent.querySelector<HTMLImageElement>('img')?.src).toBe('https://example.com/agent.png');
    expect(agent.classList.contains('is-agent')).toBe(true);
  });

  it('falls back to the identity initial when an image cannot load', () => {
    const element = createAvatarElement({
      kind: 'companion-user',
      id: 'peer-1',
      name: '小明',
      avatar: { kind: 'image', src: 'https://example.com/missing.png' },
    });
    element.querySelector('img')?.dispatchEvent(new Event('error'));
    expect(element.textContent).toBe('小');
    expect(element.querySelector('img')).toBeNull();
  });

  it('renders external identities as keycaps with a provider badge', () => {
    const element = createAvatarElement({
      kind: 'external-agent',
      id: 'codex',
      name: 'Codex',
      provider: 'codex',
      badge: 'X',
    });

    expect(element.classList.contains('mw-avatar--external-agent')).toBe(true);
    expect(element.textContent).toBe('X');
    expect(element.classList.contains('agent-provider-tone-1')).toBe(true);
  });

  it('rejects unsafe avatar sources and keeps provider tone deterministic', () => {
    expect(parseAvatarRef('/Users/secret/avatar.png')).toBeUndefined();
    expect(parseAvatarRef('javascript:alert(1)')).toBeUndefined();
    expect(avatarTone('Kimi')).toBe(0);
    expect(avatarTone('unknown-provider')).toBe(avatarTone('unknown-provider'));
  });
});
