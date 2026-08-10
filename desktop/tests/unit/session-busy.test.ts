/** @vitest-environment happy-dom */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { openTurnForRequest, resumeSessionGeneration, syncSessionLiveFromBackend } from '../../src/ui/features/session-busy';
import { __resetAllStoresForTest, messageStore, sessionStore } from '../../src/ui/stores/stores';
import { patchBook, setActiveSessionId } from '../../src/ui/state';
import { syncRunningIntroSlot } from '../../src/ui/features/running-intro';

beforeEach(() => {
  __resetAllStoresForTest();
  document.body.innerHTML = '<div class="chat-running-intro"></div>';
});

afterEach(() => {
  sessionStore.set({ busySessions: {} });
  syncRunningIntroSlot();
  vi.useRealTimers();
});

describe('syncSessionLiveFromBackend', () => {
  it('重复同步相同运行提示时保留机器人节点和动画进度', () => {
    vi.useFakeTimers();
    setActiveSessionId('sid-1');
    sessionStore.set({ busySessions: { 'sid-1': true } });

    syncRunningIntroSlot();
    const slot = document.querySelector<HTMLElement>('.chat-running-intro')!;
    const intro = slot.firstElementChild;
    const logo = slot.querySelector('.running-intro__logo');
    const logoImage = logo?.querySelector<HTMLImageElement>('.running-intro__agent-logo');

    expect(logoImage?.getAttribute('src')).toBe('./crew-jump-agent.png');
    expect(logo?.querySelector('svg')).toBeNull();

    syncRunningIntroSlot();

    expect(slot.firstElementChild).toBe(intro);
    expect(slot.querySelector('.running-intro__logo')).toBe(logo);
    expect(slot.querySelector('.running-intro__agent-logo')).toBe(logoImage);
  });

  it('live=running → busy true + turnSealed false', () => {
    syncSessionLiveFromBackend('sid-1', 'running');
    expect(sessionStore.get().busySessions['sid-1']).toBe(true);
    expect(sessionStore.get().sessionStatuses['sid-1']).toBe('running');
    expect(sessionStore.get().books['sid-1']?.turnSealed).toBe(false);
    expect(sessionStore.get().books['sid-1']?.acceptingNewRequest).toBe(true);
  });

  it('live=running with backend request id binds the active turn directly', () => {
    syncSessionLiveFromBackend('sid-1', 'running', undefined, 'req-live');
    expect(sessionStore.get().books['sid-1']?.activeRequestId).toBe('req-live');
    expect(sessionStore.get().books['sid-1']?.acceptingNewRequest).toBe(false);
  });

  it('live recovery without backend request id falls back to first-frame binding', () => {
    resumeSessionGeneration('sid-1', 'req-old');
    syncSessionLiveFromBackend('sid-1', 'idle');

    syncSessionLiveFromBackend('sid-1', 'running');

    expect(sessionStore.get().books['sid-1']?.turnSealed).toBe(false);
    expect(sessionStore.get().books['sid-1']?.activeRequestId).toBeNull();
    expect(sessionStore.get().books['sid-1']?.acceptingNewRequest).toBe(true);
  });

  it('live=idle → busy false + turnSealed true', () => {
    syncSessionLiveFromBackend('sid-1', 'idle');
    // 状态隔离后 setBusy 对「未 busy → false」短路（undefined 与 false 都表示 not busy），
    // 不再写入显式 false 键。这里断言可观察契约（not busy）即可，不绑定内部存储值。
    expect(sessionStore.get().busySessions['sid-1']).toBeFalsy();
    expect(sessionStore.get().sessionStatuses['sid-1']).toBe('idle');
    expect(sessionStore.get().books['sid-1']?.turnSealed).toBe(true);
    expect(sessionStore.get().books['sid-1']?.acceptingNewRequest).toBe(false);
  });

  it('last_status failed → session status error', () => {
    syncSessionLiveFromBackend('sid-1', undefined, 'failed');
    expect(sessionStore.get().sessionStatuses['sid-1']).toBe('error');
  });

  it('resume with explicit request id binds the next generation turn', () => {
    resumeSessionGeneration('sid-1', 'req-plan');
    expect(sessionStore.get().books['sid-1']?.activeRequestId).toBe('req-plan');
    expect(sessionStore.get().books['sid-1']?.acceptingNewRequest).toBe(false);
    expect(sessionStore.get().busySessions['sid-1']).toBe(true);
  });

  it('resume without explicit request id keeps the current request identity', () => {
    resumeSessionGeneration('sid-1', 'req-followup');
    resumeSessionGeneration('sid-1');
    expect(sessionStore.get().books['sid-1']?.activeRequestId).toBe('req-followup');
    expect(sessionStore.get().books['sid-1']?.acceptingNewRequest).toBe(false);
  });

  it('openTurnForRequest unseals, binds request id, and mounts optimistic process assistant', () => {
    openTurnForRequest('sid-1', 'req-send');
    const book = sessionStore.get().books['sid-1'];
    expect(book?.turnSealed).toBe(false);
    expect(book?.activeRequestId).toBe('req-send');
    expect(book?.acceptingNewRequest).toBe(false);
    expect(book?.assistantId).toBeTruthy();
    const msgs = messageStore.get().messages['sid-1'] ?? [];
    expect(msgs).toHaveLength(1);
    expect(msgs[0]?.id).toBe(book?.assistantId);
    expect(msgs[0]?.streaming).toBe(true);
    expect(msgs[0]?.segmentRole).toBe('process');
    expect(msgs[0]?.content).toBe('');
  });

  it('openTurnForRequest preserves approved pendingPlan for Plan Board during later turns', () => {
    patchBook('sid-1', {
      pendingPlan: {
        plan: '# 已批准\n\n执行中',
        planFile: 'plans/p.md',
        status: 'approved',
      },
    });
    openTurnForRequest('sid-1', 'req-next');
    expect(sessionStore.get().books['sid-1']?.pendingPlan).toEqual({
      plan: '# 已批准\n\n执行中',
      planFile: 'plans/p.md',
      status: 'approved',
    });
  });

  it('sync live running does not overwrite local in-flight request id', () => {
    openTurnForRequest('sid-1', 'req-local');
    syncSessionLiveFromBackend('sid-1', 'running', undefined, 'req-stale-backend');
    expect(sessionStore.get().books['sid-1']?.activeRequestId).toBe('req-local');
  });

  it('stale live running for the sealed request does not resurrect busy after final', () => {
    openTurnForRequest('sid-1', 'req-final');
    syncSessionLiveFromBackend('sid-1', 'idle', 'completed');

    syncSessionLiveFromBackend('sid-1', 'running', 'completed', 'req-final');

    expect(sessionStore.get().busySessions['sid-1']).toBeFalsy();
    expect(sessionStore.get().sessionStatuses['sid-1']).toBe('idle');
    expect(sessionStore.get().books['sid-1']?.turnSealed).toBe(true);
    expect(sessionStore.get().books['sid-1']?.activeRequestId).toBe('req-final');
  });

  it('backend live running keeps busy idle while the turn is waiting for followup input', () => {
    openTurnForRequest('sid-1', 'req-followup');
    patchBook('sid-1', {
      pendingFollowup: {
        questionId: 'q1',
        title: '需要确认',
        questions: [],
      },
    });

    syncSessionLiveFromBackend('sid-1', 'running', undefined, 'req-followup');

    expect(sessionStore.get().busySessions['sid-1']).toBeFalsy();
    expect(sessionStore.get().sessionStatuses['sid-1']).toBe('idle');
    expect(sessionStore.get().books['sid-1']?.turnSealed).toBe(true);
  });

  it('resumeSessionGeneration mounts optimistic process assistant when none is live', () => {
    resumeSessionGeneration('sid-1', 'req-resume');
    const book = sessionStore.get().books['sid-1'];
    expect(book?.assistantId).toBeTruthy();
    expect(book?.activeRequestId).toBe('req-resume');
    const msgs = messageStore.get().messages['sid-1'] ?? [];
    expect(msgs).toHaveLength(1);
    expect(msgs[0]?.streaming).toBe(true);
    expect(msgs[0]?.segmentRole).toBe('process');
  });

  it('resumeSessionGeneration does not replace an already-streaming assistant', () => {
    openTurnForRequest('sid-1', 'req-a');
    const firstId = sessionStore.get().books['sid-1']?.assistantId;
    resumeSessionGeneration('sid-1', 'req-a');
    expect(sessionStore.get().books['sid-1']?.assistantId).toBe(firstId);
    expect(messageStore.get().messages['sid-1']).toHaveLength(1);
  });
});
