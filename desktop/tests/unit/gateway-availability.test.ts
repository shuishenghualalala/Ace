import { describe, expect, it, vi } from 'vitest';

import {
  chooseStandaloneGatewayAction,
  nextGatewayConnectionState,
  waitForGatewayCandidate,
} from '../../src/main/gateway-availability';
import type { GatewayInstanceProbe } from '../../src/main/gateway-instance-auth';

describe('chooseStandaloneGatewayAction', () => {
  it('never starts a managed replacement while a standalone listener exists', () => {
    expect(chooseStandaloneGatewayAction(
      { status: 'unreachable', verified: false },
      true,
    )).toBe('wait');
    expect(chooseStandaloneGatewayAction(
      { status: 'untrusted', verified: false },
      true,
    )).toBe('reject');
  });

  it('reuses a verified Gateway even when its security state is not ready', () => {
    expect(chooseStandaloneGatewayAction(
      {
        status: 'verified',
        verified: true,
        components: { security_state: { status: 'ready' } },
      },
      true,
    )).toBe('reuse');
    expect(chooseStandaloneGatewayAction(
      {
        status: 'verified',
        verified: true,
        components: { security_state: { status: 'failed' } },
      },
      true,
    )).toBe('reuse');
    expect(chooseStandaloneGatewayAction(
      { status: 'unreachable', verified: false },
      false,
    )).toBe('start-managed');
  });
});

describe('waitForGatewayCandidate', () => {
  it('waits through transient timeouts and accepts the same candidate when it recovers', async () => {
    const probes: GatewayInstanceProbe[] = [
      { status: 'unreachable', verified: false },
      { status: 'unreachable', verified: false },
      { status: 'verified', verified: true },
    ];
    const sleep = vi.fn(async () => undefined);

    const result = await waitForGatewayCandidate({
      probe: async () => probes.shift()!,
      shouldContinue: async () => true,
      sleep,
      retryDelayMs: 10,
    });

    expect(result.status).toBe('ready');
    expect(sleep).toHaveBeenCalledTimes(2);
  });

  it('rejects an explicitly untrusted listener without retrying it', async () => {
    const sleep = vi.fn(async () => undefined);

    const result = await waitForGatewayCandidate({
      probe: async () => ({ status: 'untrusted', verified: false }),
      shouldContinue: async () => true,
      sleep,
    });

    expect(result.status).toBe('untrusted');
    expect(sleep).not.toHaveBeenCalled();
  });

  it('stops waiting for an unreachable candidate after a bounded timeout', async () => {
    let now = 0;
    const sleep = vi.fn(async (delayMs: number) => {
      now += delayMs;
    });

    const result = await waitForGatewayCandidate({
      probe: async () => ({ status: 'unreachable', verified: false }),
      shouldContinue: async () => true,
      sleep,
      retryDelayMs: 10,
      maxWaitMs: 25,
      now: () => now,
    });

    expect(result.status).toBe('timeout');
    expect(sleep).toHaveBeenCalled();
  });
});

describe('nextGatewayConnectionState', () => {
  it('preserves a verified connection while its listener is temporarily busy', () => {
    expect(nextGatewayConnectionState(
      true,
      { status: 'unreachable', verified: false },
      true,
    )).toBe(true);
  });

  it('disconnects when the listener disappears or becomes untrusted', () => {
    expect(nextGatewayConnectionState(
      true,
      { status: 'unreachable', verified: false },
      false,
    )).toBe(false);
    expect(nextGatewayConnectionState(
      true,
      { status: 'untrusted', verified: false },
      true,
    )).toBe(false);
  });
});
