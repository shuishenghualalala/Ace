/**
 * @vitest-environment happy-dom
 */
import { afterEach, describe, expect, it } from 'vitest';
import { createCompanionComposerPresence } from '../../src/ui/features/companion-composer-presence';
import { conversationAdapters } from '../../src/ui/features/conversation-adapters';
import { __resetAllStoresForTest, sessionStore } from '../../src/ui/stores/stores';

describe('Companion Composer presence', () => {
  let unregister: (() => void) | null = null;

  afterEach(() => {
    unregister?.();
    unregister = null;
    document.body.replaceChildren();
    __resetAllStoresForTest();
  });

  it('is hidden outside a room and expands into member and Agent states', () => {
    const sessionId = 'agent:main:nearby:room:test';
    unregister = conversationAdapters.register({
      id: 'presence-test',
      matches: (candidate) => candidate === sessionId,
      abilities: () => ({
        canSendText: true,
        canAttach: false,
        canMentionPeople: true,
        canMentionAgents: true,
        showModelPicker: false,
        showSkills: false,
        showPlanMode: false,
      }),
      composerContext: () => ({
        title: '同伴产品小队',
        modeLabel: '@触发',
        members: [{
          peerId: 'ace_peer_a',
          label: '林墨',
          isSelf: false,
          state: 'online',
          stateLabel: '在线',
          agents: [{
            kind: 'agent',
            peerId: 'ace_peer_a',
            publicAgentId: 'agent_mori',
            label: 'Mori',
            ownerLabel: '林墨',
            routing: 'specific',
            state: 'working',
            stateLabel: '处理中',
          }],
        }],
      }),
      send: async () => undefined,
    });
    sessionStore.set({ activeSessionId: sessionId });
    const host = document.createElement('div');
    document.body.append(host);
    const presence = createCompanionComposerPresence(host, () => sessionStore.get().activeSessionId);

    const toggle = host.querySelector<HTMLButtonElement>('.companion-presence__toggle');
    expect(toggle?.textContent).toContain('同伴产品小队');
    expect(toggle?.textContent).toContain('1/1 人在线');
    expect(toggle?.getAttribute('aria-expanded')).toBe('false');
    toggle?.click();
    expect(host.querySelector('.companion-presence__members')).not.toBeNull();
    expect(host.querySelector('[aria-label="Mori，处理中，主人 林墨"]')).not.toBeNull();

    presence.dispose();
  });
});
