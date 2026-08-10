import { describe, expect, it } from 'vitest';

import {
  managedGatewayModeEnv,
  resolveGatewayCrewHome,
  resolveGatewayIdentityMode,
  shouldProbeExternalGateway,
} from '../../src/main/gateway-launch-mode';

describe('resolveGatewayIdentityMode', () => {
  it('uses local mode for normal launches', () => {
    expect(resolveGatewayIdentityMode(false)).toBe('local');
  });

  it('keeps the local Gateway identity for development launches', () => {
    expect(resolveGatewayIdentityMode(true)).toBe('local');
  });
});

describe('Gateway launch policy', () => {
  it('keeps local mode on the normal environment and allows verified Gateway reuse', () => {
    expect(resolveGatewayCrewHome('local', 'C:/local-home', 'C:/dev-home'))
      .toBe('C:/local-home');
    expect(managedGatewayModeEnv('local', 'C:/dev-home')).toEqual({});
    expect(shouldProbeExternalGateway('local')).toBe(true);
  });

  it('lets development launches reuse the verified local Gateway', () => {
    const mode = resolveGatewayIdentityMode(true);
    expect(resolveGatewayCrewHome(mode, 'C:/account-home', 'C:/dev-home'))
      .toBe('C:/account-home');
    expect(managedGatewayModeEnv(mode, 'C:/dev-home')).toEqual({});
    expect(shouldProbeExternalGateway(mode)).toBe(true);
  });
});
