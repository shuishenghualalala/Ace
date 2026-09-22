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

    // 首次重连延迟 = 1.5s × [0.8, 1.2] 抖动 → 2s 内必然触发。
    await vi.advanceTimersByTimeAsync(2_000);
    expect(connect).toHaveBeenCalledTimes(2);
    vi.useRealTimers();
  });

  it('malformed proxy frame is dropped without throwing into the event callback', () => {
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

    const chunks: unknown[] = [];
    const sock = new BackendChatSocket((chunk) => chunks.push(chunk), () => {});
    sock.connect();
    events[0]({ type: 'open' });
    // 非法 JSON 帧：丢弃 + 上报，不抛出。
    expect(() => events[0]({ type: 'message', data: '{not json' })).not.toThrow();
    expect(chunks).toHaveLength(0);
  });

  it('reconnect backoff grows and resets after a successful open', async () => {
    vi.useFakeTimers();
    vi.spyOn(Math, 'random').mockReturnValue(0); // 固定抖动因子 0.8，延迟可精确断言
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
    events[0]({ type: 'open' }); // attempts 清零

    // 连续断连：第 1 次 1.5s×0.8=1200ms，第 2 次 3s×0.8=2400ms，第 3 次 6s×0.8=4800ms。
    events[0]({ type: 'close', code: 1006, reason: '' });
    await vi.advanceTimersByTimeAsync(1_200);
    expect(connect).toHaveBeenCalledTimes(2);
    events[0]({ type: 'close', code: 1006, reason: '' });
    await vi.advanceTimersByTimeAsync(2_400);
    expect(connect).toHaveBeenCalledTimes(3);
    events[0]({ type: 'close', code: 1006, reason: '' });
    await vi.advanceTimersByTimeAsync(4_800);
    expect(connect).toHaveBeenCalledTimes(4);

    // 重新 open 后退避清零：下一次断连回到 1200ms。
    events[0]({ type: 'open' });
    events[0]({ type: 'close', code: 1006, reason: '' });
    await vi.advanceTimersByTimeAsync(1_200);
    expect(connect).toHaveBeenCalledTimes(5);
    vi.mocked(Math.random).mockRestore();
    vi.useRealTimers();
  });
});
