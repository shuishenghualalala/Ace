import { describe, expect, it } from 'vitest';
import { GatewayWsProtocolIdentity } from '../../src/shared/gateway-ws-protocol';

describe('Gateway WebSocket protocol identity', () => {
  it('overrides caller fields and advances the sequence', () => {
    let nonceIndex = 0;
    const identity = new GatewayWsProtocolIdentity(
      () => `0000000000000000000000000000000${++nonceIndex}`,
    );

    expect(identity.frame({
      kind: 'pong',
      protocol_version: 99,
      client_sequence: 99,
      nonce: 'caller-controlled',
    })).toEqual({
      kind: 'pong',
      protocol_version: 1,
      client_sequence: 1,
      nonce: '00000000000000000000000000000001',
    });
    expect(identity.frame({ kind: 'pong' })).toEqual({
      kind: 'pong',
      protocol_version: 1,
      client_sequence: 2,
      nonce: '00000000000000000000000000000002',
    });
  });

  it('resets sequence state for a replacement socket', () => {
    const identity = new GatewayWsProtocolIdentity(
      () => '00000000000000000000000000000001',
    );
    identity.frame({ kind: 'pong' });

    identity.reset();

    expect(identity.frame({ kind: 'pong' }).client_sequence).toBe(1);
  });
});
