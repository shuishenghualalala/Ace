// @vitest-environment happy-dom

import { expect, it, vi } from 'vitest';
import type { AuthStateSnapshot } from '../../src/shared/types';
import { initAuthFlow } from '../../src/ui/features/login';
import { bindBackendAuth } from '../../src/ui/features/session-controller';
import { __resetAllStoresForTest, authStore } from '../../src/ui/stores/stores';
import { state } from '../../src/ui/state';

it('boots chat once on a verified account push and ignores the stale initial auth snapshot', async () => {
  __resetAllStoresForTest();
  const initial = Promise.withResolvers<{ ok: boolean; state: AuthStateSnapshot }>();
  const connect = vi.fn().mockResolvedValue({ ok: true });
  const close = vi.fn().mockResolvedValue({ ok: true });
  let emitAuth!: (state: AuthStateSnapshot) => void;
  vi.stubGlobal('Crew', {
    authGetState: () => initial.promise,
    onSessionState: (callback: (state: AuthStateSnapshot) => void) => { emitAuth = callback; },
    rendererInitialStateReady: vi.fn().mockResolvedValue({ ok: true }),
    gatewayWsConnect: connect,
    gatewayWsSend: vi.fn().mockResolvedValue({ ok: true }),
    gatewayWsClose: close,
    onGatewayWsEvent: vi.fn(() => () => {}),
  });
  const authInitialized = initAuthFlow();
  const dispose = bindBackendAuth();
  try {
    expect(connect).not.toHaveBeenCalled();
    expect(authStore.get().isLoggedIn).toBe(false);
    const unknown: AuthStateSnapshot = {
      mode: 'unknown', configured: false, providerId: 'custom', isLoggedIn: true, user: null,
    };
    emitAuth(unknown);
    expect(connect).not.toHaveBeenCalled();
    expect(authStore.get().isLoggedIn).toBe(false);

    const verified: AuthStateSnapshot = {
      mode: 'local', configured: true, providerId: 'local', isLoggedIn: true, user: null,
    };
    emitAuth(verified);
    emitAuth(verified);
    expect(connect).toHaveBeenCalledOnce();
    expect(state.socket).toBeTruthy();
    initial.resolve({ ok: true, state: { ...unknown, isLoggedIn: false } });
    await expect(authInitialized).resolves.toBe(true);
    expect(authStore.get().isLoggedIn).toBe(true);

    emitAuth({ ...verified, isLoggedIn: false });
    expect(close).toHaveBeenCalledOnce();
    expect(state.socket).toBeNull();
  } finally {
    dispose();
    vi.unstubAllGlobals();
  }
});
