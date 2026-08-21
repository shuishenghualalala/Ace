import { describe, expect, it, vi } from 'vitest';
import {
  TRAY_REST_TIMEOUT_MS,
  pendingAttentionKeys,
  resolveTrayStatus,
} from '../../src/ui/features/system-tray';
import { trayIconOpticalScale } from '../../src/main/tray-service';

vi.mock('electron', () => ({
  Menu: { buildFromTemplate: vi.fn() },
  nativeImage: {},
  Tray: class {},
}));

describe('system tray status', () => {
  it('keeps the approved priority order', () => {
    expect(resolveTrayStatus({
      hasNotification: true,
      hasWorkingSession: true,
      hasDoneSession: true,
      idleForMs: TRAY_REST_TIMEOUT_MS,
    })).toBe('notification');
    expect(resolveTrayStatus({
      hasNotification: false,
      hasWorkingSession: true,
      hasDoneSession: true,
      idleForMs: TRAY_REST_TIMEOUT_MS,
    })).toBe('working');
    expect(resolveTrayStatus({
      hasNotification: false,
      hasWorkingSession: false,
      hasDoneSession: true,
      idleForMs: TRAY_REST_TIMEOUT_MS,
    })).toBe('done');
    expect(resolveTrayStatus({
      hasNotification: false,
      hasWorkingSession: false,
      hasDoneSession: false,
      idleForMs: TRAY_REST_TIMEOUT_MS,
    })).toBe('rest');
    expect(resolveTrayStatus({
      hasNotification: false,
      hasWorkingSession: false,
      hasDoneSession: false,
      idleForMs: 0,
    })).toBe('default');
  });

  it('treats pending plan and follow-up as attention states', () => {
    const keys = pendingAttentionKeys({
      busySessions: {},
      sessionStatuses: {},
      unreadCompletedSessions: new Set(),
      books: {
        'session-a': {
          pendingPlan: { plan: 'plan', planFile: '', status: 'pending' },
          pendingFollowup: {
            questionId: 'question-1',
            title: '需要确认',
            recordHistory: true,
            questions: [],
          },
        },
      },
    } as never);

    expect(keys).toEqual(['plan:session-a', 'followup:session-a:question-1']);
  });

  it('optically enlarges the small done artwork without changing template states', () => {
    expect(trayIconOpticalScale('default')).toBe(1);
    expect(trayIconOpticalScale('rest')).toBe(1);
    expect(trayIconOpticalScale('done')).toBeGreaterThan(trayIconOpticalScale('working'));
    expect(trayIconOpticalScale('done')).toBe(1.16);
  });
});
