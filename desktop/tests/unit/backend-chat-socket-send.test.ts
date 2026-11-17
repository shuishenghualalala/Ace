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

  it('includes versioned cursors alongside legacy positions on subscribe and reconnect', async () => {
    let emit!: (event: unknown) => void;
    const send = vi.fn().mockResolvedValue({ ok: true });
    vi.stubGlobal('Crew', {
      gatewayWsConnect: vi.fn().mockResolvedValue({ ok: true }),
      gatewayWsSend: send,
      gatewayWsClose: vi.fn().mockResolvedValue({ ok: true }),
      onGatewayWsEvent: (listener: typeof emit) => {
        emit = listener;
        return () => {};
      },
    });
    const socket = new BackendChatSocket(() => {}, () => {});
    socket.bindLastGatewaySequences(() => ({ a: 11, b: 5 }), () => ({ a: { epoch: 'epoch', sequence: 11 } }));
    try {
      socket.connect();
      emit({ type: 'open' });
      await socket.subscribe(['a', 'b'], { a: 11, b: 5 });
      const expected = {
        action: 'subscribe', session_id: 'a', sessions: ['a', 'b'],
        last_gateway_sequences: { a: 11, b: 5 },
        gateway_cursors: { a: { epoch: 'epoch', sequence: 11 } },
      };
      expect(send).toHaveBeenLastCalledWith(expected);
      emit({ type: 'close', reason: 'reconnect' });
      emit({ type: 'open' });
      expect(send).toHaveBeenCalledTimes(2);
      expect(send).toHaveBeenLastCalledWith(expected);
    } finally {
      socket.dispose();
      vi.unstubAllGlobals();
    }
  });
});
