// @vitest-environment happy-dom

import { describe, expect, it, vi } from 'vitest';
import { BackendChatSocket } from '../../src/ui/backend-client';

describe('BackendChatSocket connect', () => {
  it('skips duplicate gatewayWsConnect when proxy is already open', async () => {
    const events: Array<(event: unknown) => void> = [];
    const connect = vi.fn().mockResolvedValue({ ok: true });
    Object.defineProperty(window, 'Crew', {
      configurable: true,
      value: {
        gatewayWsConnect: connect,
        gatewayWsSend: vi.fn().mockResolvedValue({ ok: true }),
        gatewayWsClose: vi.fn().mockResolvedValue({ ok: true }),
        onGatewayWsEvent: vi.fn((cb: (event: unknown) => void) => {
          events.push(cb);
          return () => {};
        }),
      },
    });

    const sock = new BackendChatSocket(() => {}, () => {});
    sock.connect();
    events[0]({ type: 'open' });
    expect(connect).toHaveBeenCalledTimes(1);
    expect(sock.isGatewayProxyOpen()).toBe(true);

    sock.connect();
    expect(connect).toHaveBeenCalledTimes(1);
  });

  it('does not schedule reconnect after transient close reason=reconnect', () => {
    vi.useFakeTimers();
    const events: Array<(event: unknown) => void> = [];
    Object.defineProperty(window, 'Crew', {
      configurable: true,
      value: {
        gatewayWsConnect: vi.fn().mockResolvedValue({ ok: true }),
        gatewayWsSend: vi.fn().mockResolvedValue({ ok: true }),
        gatewayWsClose: vi.fn().mockResolvedValue({ ok: true }),
        onGatewayWsEvent: vi.fn((cb: (event: unknown) => void) => {
          events.push(cb);
          return () => {};
        }),
      },
    });

    const statuses: Array<{ open: boolean; transient?: boolean }> = [];
    const sock = new BackendChatSocket(
      () => {},
      (open, meta) => statuses.push({ open, transient: meta?.transient }),
    );
    sock.connect();
    events[0]({ type: 'open' });
    events[0]({ type: 'close', code: 1000, reason: 'reconnect' });

    vi.advanceTimersByTime(2000);
    expect(window.Crew!.gatewayWsConnect).toHaveBeenCalledTimes(1);
    expect(statuses.at(-1)).toEqual({ open: false, transient: true });
    vi.useRealTimers();
  });

  it('retries gatewayWsConnect after proxy connect failure', async () => {
    vi.useFakeTimers();
    const connect = vi.fn()
      .mockResolvedValueOnce({ ok: false, error: 'gateway not ready' })
      .mockResolvedValue({ ok: true });
    Object.defineProperty(window, 'Crew', {
      configurable: true,
      value: {
        gatewayWsConnect: connect,
        gatewayWsSend: vi.fn().mockResolvedValue({ ok: true }),
        gatewayWsClose: vi.fn().mockResolvedValue({ ok: true }),
        onGatewayWsEvent: vi.fn(() => () => {}),
      },
    });

    const sock = new BackendChatSocket(() => {}, () => {});
    sock.connect();
    await Promise.resolve();
    expect(connect).toHaveBeenCalledTimes(1);

    await vi.advanceTimersByTimeAsync(1500);
    expect(connect).toHaveBeenCalledTimes(2);
    vi.useRealTimers();
  });
});
