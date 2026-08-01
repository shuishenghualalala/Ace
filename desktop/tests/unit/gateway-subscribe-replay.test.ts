import { describe, it, expect, vi } from 'vitest';
import { BackendChatSocket } from '../../src/ui/backend-client';

describe('BackendChatSocket subscribe replay', () => {
  it('subscribe sends last_gateway_sequences when provided', () => {
    const sock = new BackendChatSocket(() => {}, () => {});
    const sendSpy = vi.spyOn(sock, 'send').mockResolvedValue(true);

    sock.subscribe(['a'], { a: 12, b: 3 });
    expect(sendSpy).toHaveBeenCalledWith({
      action: 'subscribe',
      session_id: 'a',
      sessions: ['a'],
      last_gateway_sequences: { a: 12, b: 3 },
    });
  });

  it('subscribe omits empty last_gateway_sequences object', () => {
    const sock = new BackendChatSocket(() => {}, () => {});
    const sendSpy = vi.spyOn(sock, 'send').mockResolvedValue(true);

    sock.subscribe(['a'], {});
    expect(sendSpy).toHaveBeenCalledWith({
      action: 'subscribe',
      session_id: 'a',
      sessions: ['a'],
      last_gateway_sequences: undefined,
    });
  });

  it('bindLastGatewaySequences feeds resubscribe payload', () => {
    const sock = new BackendChatSocket(() => {}, () => {});
    sock.bindLastGatewaySequences((ids) => Object.fromEntries(ids.map((id, i) => [id, (i + 1) * 10])));
    const sendSpy = vi.spyOn(sock, 'send').mockResolvedValue(true);
    sock.subscribe(['a', 'b']);
    sendSpy.mockClear();
    (sock as unknown as { resubscribe(): void }).resubscribe();
    expect(sendSpy).toHaveBeenCalledWith(
      expect.objectContaining({
        action: 'subscribe',
        last_gateway_sequences: { a: 10, b: 20 },
      }),
    );
  });
});
