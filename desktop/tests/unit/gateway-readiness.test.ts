import { describe, expect, it } from 'vitest';
import { GatewayReadinessTracker, GatewayStartupError, readinessFromMainProbe } from '../../src/main/gateway-readiness';

describe('desktop Gateway readiness', () => {
  it('requires a report from the proved main listener for a managed launch', () => {
    expect(readinessFromMainProbe({ verified: false }, true)).toBeUndefined();
    expect(() => readinessFromMainProbe({
      verified: true, components: { startup: { status: 'ready' } },
    }, true)).toThrow(GatewayStartupError);
    expect(readinessFromMainProbe({ verified: true }, false)).toBeNull();
    expect(readinessFromMainProbe({
      verified: true, readiness: { status: 'starting', generation: 'core-attempt' },
    }, true)).toEqual({ status: 'starting', generation: 'core-attempt' });
  });

  it('rejects late readiness from the old launch even after a later retry succeeds', () => {
    const state = new GatewayReadinessTracker();
    expect(state.observe(0, 'failed', 'GATEWAY_CORE_STARTUP_FAILED')).toBe(true);
    expect(state.observe(0, 'ready')).toBe(false);
    state.reset(1);
    expect(state.observe(0, 'ready')).toBe(false);
    expect(state.snapshot).toEqual({ status: 'starting', generation: 1 });
    expect(state.observe(1, 'ready')).toBe(true);
    expect(state.observe(0, 'failed')).toBe(false);
    expect(state.observe(1, 'starting')).toBe(false);
    expect(state.snapshot).toEqual({ status: 'ready', generation: 1 });
  });
});
