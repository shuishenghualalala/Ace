/**
 * @vitest-environment happy-dom
 *
 * BackendChatSocket 订阅增量单测。
 *
 * 关键约束（实现决策）：gateway (crew/gateway/ws.py) 不支持 unsubscribe 动作，
 * 且未识别动作会被当作空 query 的对话回合 _spawn → unsubscribe 仅在客户端裁剪 Set，
 * 不向 gateway 发消息。重连时 resubscribe() 只发剩余会话，gateway 状态收敛。
 */
import { beforeEach, describe, it, expect, vi } from 'vitest';
import { BackendChatSocket } from '../../src/ui/backend-client';

class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  static OPEN = 1;

  readyState = FakeWebSocket.OPEN;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: (() => void) | null = null;
  sent: string[] = [];

  constructor(_url: string) {
    FakeWebSocket.instances.push(this);
  }

  send(payload: string): void {
    this.sent.push(payload);
  }

  close(): void {
    this.readyState = 3;
    this.onclose?.();
  }

  emitOpen(): void {
    this.onopen?.();
  }

  emitMessage(payload: unknown): void {
    this.onmessage?.({ data: JSON.stringify(payload) });
  }
}

beforeEach(() => {
  FakeWebSocket.instances = [];
});

describe('BackendChatSocket subscription', () => {
  it('subscribe adds to set + sends subscribe action with the ids', () => {
    const sock = new BackendChatSocket(() => {}, () => {});
    const sendSpy = vi.spyOn(sock, 'send').mockResolvedValue(true);

    sock.subscribe(['a', 'b']);
    expect(sendSpy).toHaveBeenCalledTimes(1);
    // 空序列时省略 last_gateway_sequences（gateway ws.py 对 None/{} 同效；与
    // gateway-subscribe-replay 的 omit 契约一致）。
    const payload = sendSpy.mock.calls[0][0] as Record<string, unknown>;
    expect(payload).toMatchObject({ action: 'subscribe', sessions: ['a', 'b'] });
    expect(payload.last_gateway_sequences).toBeUndefined();
    expect(sock.getSubscribedSessions()).toEqual(['a', 'b']);
  });

  it('unsubscribe prunes local set WITHOUT sending to gateway', () => {
    const sock = new BackendChatSocket(() => {}, () => {});
    const sendSpy = vi.spyOn(sock, 'send').mockResolvedValue(true);

    sock.subscribe(['a', 'b']);
    sendSpy.mockClear();

    sock.unsubscribe(['a']);
    // 不能发 unsubscribe：gateway 会把它当空 query 的对话回合
    expect(sendSpy).not.toHaveBeenCalled();
    expect(sock.getSubscribedSessions()).toEqual(['b']);
  });

  it('subscribe after unsubscribe only sends newly added ids', () => {
    const sock = new BackendChatSocket(() => {}, () => {});
    const sendSpy = vi.spyOn(sock, 'send').mockResolvedValue(true);

    sock.subscribe(['a', 'b']);
    sock.unsubscribe(['a']);
    sendSpy.mockClear();

    sock.subscribe(['c']);
    expect(sendSpy).toHaveBeenCalledTimes(1);
    // 同上：空序列时省略 last_gateway_sequences。
    const payload = sendSpy.mock.calls[0][0] as Record<string, unknown>;
    expect(payload).toMatchObject({ action: 'subscribe', sessions: ['c'] });
    expect(payload.last_gateway_sequences).toBeUndefined();
    expect(sock.getSubscribedSessions()).toEqual(['b', 'c']);
  });

  it('unsubscribe tolerates unknown / empty ids (idempotent)', () => {
    const sock = new BackendChatSocket(() => {}, () => {});
    vi.spyOn(sock, 'send').mockResolvedValue(true);
    sock.subscribe(['a']);
    expect(() => sock.unsubscribe(['x', '', '   '])).not.toThrow();
    expect(sock.getSubscribedSessions()).toEqual(['a']);
  });

  it('dispose clears the subscription set', () => {
    const sock = new BackendChatSocket(() => {}, () => {});
    vi.spyOn(sock, 'send').mockResolvedValue(true);
    sock.subscribe(['a', 'b']);
    sock.dispose();
    expect(sock.getSubscribedSessions()).toEqual([]);
  });

  it('forwards gateway_sequence frames and reports injected max sequence on subscribe', () => {
    const originalWebSocket = globalThis.WebSocket;
    const originalLocalStorage = globalThis.localStorage;
    vi.stubGlobal('WebSocket', FakeWebSocket);
    vi.stubGlobal('localStorage', {
      getItem: vi.fn(() => ''),
      setItem: vi.fn(),
      removeItem: vi.fn(),
      clear: vi.fn(),
    });
    try {
      const onChunk = vi.fn();
      const sock = new BackendChatSocket(onChunk, () => {});
      sock.connect();
      const ws = FakeWebSocket.instances[0]!;
      ws.emitOpen();

      ws.emitMessage({
        kind: 'delta',
        body: { text: 'A' },
        is_final: false,
        sequence: 1,
        session_id: 's1',
        gateway_sequence: 7,
      });
      ws.emitMessage({
        kind: 'delta',
        body: { text: 'A duplicate' },
        is_final: false,
        sequence: 1,
        session_id: 's1',
        gateway_sequence: 7,
      });
      ws.emitMessage({
        kind: 'delta',
        body: { text: 'B' },
        is_final: false,
        sequence: 2,
        session_id: 's1',
        gateway_sequence: 8,
      });
      ws.emitMessage({
        kind: 'tool',
        body: { name: 'todo', action: 'update' },
        is_final: false,
        sequence: 3,
        session_id: 's1',
        gateway_sequence: 6,
      });
      ws.emitMessage({
        kind: 'tool',
        body: { name: 'todo', action: 'duplicate' },
        is_final: false,
        sequence: 4,
        session_id: 's1',
        gateway_sequence: 6,
      });

      expect(onChunk).toHaveBeenCalledTimes(5);
      expect(onChunk.mock.calls.map((call) => call[0].body)).toEqual([
        { text: 'A' },
        { text: 'A duplicate' },
        { text: 'B' },
        { name: 'todo', action: 'update' },
        { name: 'todo', action: 'duplicate' },
      ]);

      const sendSpy = vi.spyOn(sock, 'send').mockResolvedValue(true);
      sock.subscribe(['s1'], { s1: 8 });
      expect(sendSpy.mock.calls[0][0]).toMatchObject({
        action: 'subscribe',
        sessions: ['s1'],
        last_gateway_sequences: { s1: 8 },
      });
      sock.dispose();
    } finally {
      vi.stubGlobal('WebSocket', originalWebSocket);
      vi.stubGlobal('localStorage', originalLocalStorage);
    }
  });
});
