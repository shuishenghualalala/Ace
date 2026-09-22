/**
 * @vitest-environment happy-dom
 *
 * 流式 watchdog 升级可见（P0-3）单测：
 * 停滞 → 静默自愈（两次）→ 自愈无效 → 写入可见错误消息并停止自动重试；
 * 出现晚于自愈动作的新活动或会话转空闲后自动复位。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mockSessionStatus = vi.hoisted(() => vi.fn());

vi.mock('../../src/ui/backend-client', () => ({
  backendApi: {
    sessionStatus: mockSessionStatus,
  },
}));

import {
  _resetWatchdogForTests,
  _watchdogEscalatedForTests,
  startStreamWatchdog,
  stopStreamWatchdog,
} from '../../src/ui/features/stream-watchdog';
import { messageStore, sessionStore, __resetAllStoresForTest } from '../../src/ui/stores/stores';
import { touchStreamActivity } from '../../src/ui/features/gateway-sequence';

describe('stream-watchdog 升级可见', () => {
  beforeEach(() => {
    __resetAllStoresForTest();
    _resetWatchdogForTests();
    mockSessionStatus.mockReset();
    // 后端报告 running → 自愈走 resubscribe-replay 分支（touch 活动 + 重订阅）。
    mockSessionStatus.mockResolvedValue({ live: 'running', last_status: 'running' });
    vi.useFakeTimers();
  });

  afterEach(() => {
    stopStreamWatchdog();
    _resetWatchdogForTests();
    vi.useRealTimers();
  });

  function armStalledSession(sid: string): void {
    const nextSubscribed = new Set(sessionStore.get().subscribedSessions);
    nextSubscribed.add(sid);
    sessionStore.set({ subscribedSessions: nextSubscribed });
    const nextBusy = { ...sessionStore.get().busySessions, [sid]: true };
    sessionStore.set({ busySessions: nextBusy });
    sessionStore.set({
      books: {
        ...sessionStore.get().books,
        [sid]: {
          ...(sessionStore.get().books[sid] ?? {}),
          firstChunkAt: Date.now() - 120_000,
        },
      },
    });
    messageStore.set({ messages: { ...messageStore.get().messages, [sid]: [] } });
  }

  it('停滞两轮自愈无效后写入一次可见错误，不再自动重试', async () => {
    armStalledSession('sid-w');
    startStreamWatchdog();

    // 第 1 轮停滞：静默自愈（resubscribe 分支 touch 活动）。
    await vi.advanceTimersByTimeAsync(15_000);
    expect(mockSessionStatus).toHaveBeenCalledTimes(1);
    expect(_watchdogEscalatedForTests().has('sid-w')).toBe(false);

    // 自愈后 60s 内活动时间戳被 touch 推进 → 不算真实新活动，停滞复发。
    await vi.advanceTimersByTimeAsync(60_000 + 15_000);
    expect(mockSessionStatus).toHaveBeenCalledTimes(2);
    expect(_watchdogEscalatedForTests().has('sid-w')).toBe(false);

    // 第三轮停滞：升级可见，写入错误消息并停止自动重试。
    await vi.advanceTimersByTimeAsync(60_000 + 15_000);
    expect(_watchdogEscalatedForTests().has('sid-w')).toBe(true);
    const messages = messageStore.get().messages['sid-w'];
    expect(messages.filter((m) => m.role === 'error')).toHaveLength(1);

    const statusCallsAfterEscalation = mockSessionStatus.mock.calls.length;
    await vi.advanceTimersByTimeAsync(120_000);
    expect(mockSessionStatus.mock.calls.length).toBe(statusCallsAfterEscalation);
  });

  it('自愈后出现真实新活动则复位，不升级', async () => {
    armStalledSession('sid-ok');
    startStreamWatchdog();

    await vi.advanceTimersByTimeAsync(15_000); // 第 1 轮自愈
    // 「晚于自愈动作」的新活动：推进 5ms 后再 touch。
    await vi.advanceTimersByTimeAsync(5);
    touchStreamActivity('sid-ok');
    await vi.advanceTimersByTimeAsync(30_000);
    expect(_watchdogEscalatedForTests().has('sid-ok')).toBe(false);
    expect(messageStore.get().messages['sid-ok'].filter((m) => m.role === 'error')).toHaveLength(0);
  });

  it('会话转入空闲后复位升级态', async () => {
    armStalledSession('sid-idle');
    startStreamWatchdog();

    await vi.advanceTimersByTimeAsync(2 * (60_000 + 15_000) + 15_000);
    expect(_watchdogEscalatedForTests().has('sid-idle')).toBe(true);

    const nextBusy = { ...sessionStore.get().busySessions };
    delete nextBusy['sid-idle'];
    sessionStore.set({ busySessions: nextBusy });
    await vi.advanceTimersByTimeAsync(15_000);
    expect(_watchdogEscalatedForTests().has('sid-idle')).toBe(false);
  });
});
