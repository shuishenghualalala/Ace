// @vitest-environment happy-dom

import { describe, expect, it, vi } from 'vitest';
import { BackendChatSocket } from '../../src/ui/backend-client';

describe('BackendChatSocket send', () => {
  it('returns false when the desktop gateway proxy rejects the send', async () => {
    const events: Array<(event: unknown) => void> = [];
    const statuses: boolean[] = [];
    Object.defineProperty(window, 'Crew', {
      configurable: true,
      value: {
        gatewayWsConnect: vi.fn().mockResolvedValue({ ok: true }),
        gatewayWsSend: vi.fn().mockResolvedValue({ ok: false, error: 'closed' }),
        gatewayWsClose: vi.fn().mockResolvedValue({ ok: true }),
        onGatewayWsEvent: vi.fn((cb: (event: unknown) => void) => {
          events.push(cb);
          return () => {};
        }),
      },
    });

    const sock = new BackendChatSocket(() => {}, (open) => statuses.push(open));
    sock.connect();
    events[0]({ type: 'open' });

    await expect(sock.send({ kind: 'pong' })).resolves.toBe(false);
    expect(statuses.at(-1)).toBe(false);
  });
});
