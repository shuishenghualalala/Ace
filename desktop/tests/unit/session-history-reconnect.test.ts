/**
 * @vitest-environment happy-dom
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { backendApi } from '../../src/ui/backend-client';
import { bootstrapBackend, shouldSkipHistoryReloadOnReconnect } from '../../src/ui/features/session-controller';
import { createSessionHistoryView } from '../../src/ui/features/session-history-view';
import { openTurnForRequest } from '../../src/ui/features/session-busy';
import { stopGeneration } from '../../src/ui/features/chat-controller';
import { stopStreamWatchdog } from '../../src/ui/features/stream-watchdog';
import { authStore } from '../../src/ui/stores/auth-store';
import {
  __resetAllStoresForTest,
  configStore,
  messageStore,
  sessionStore,
} from '../../src/ui/stores/stores';
import { addSubscribedSessions, ensureSessionBook, patchBook, setBusy, setActiveSessionId, state } from '../../src/ui/state';

beforeEach(() => {
  __resetAllStoresForTest();
  document.body.innerHTML = '';
  setActiveSessionId('sid-1');
});

describe('conversation recovery after a socket outage', () => {
  const sid = 'sid-1';
  const oldRequest = 'req-before-disconnect';
  const settledStatus = {
    session_id: sid,
    live: 'idle',
    queue_depth: 0,
    last_status: 'completed',
    last_error: '',
  };
  let emitSocket: (event: unknown) => void;

  beforeEach(() => {
    vi.useFakeTimers();
    authStore.set({ isLoggedIn: true });
    vi.stubGlobal('Crew', {
      gatewayWsConnect: vi.fn().mockResolvedValue({ ok: true }),
      gatewayWsSend: vi.fn().mockResolvedValue({ ok: true }),
      gatewayWsClose: vi.fn().mockResolvedValue({ ok: true }),
      onGatewayWsEvent: (onEvent: (event: unknown) => void) => {
        emitSocket = onEvent;
        return () => {};
      },
    });
    openTurnForRequest(sid, oldRequest);
    setBusy(sid, true);
    addSubscribedSessions([sid]);
    bootstrapBackend();
    vi.spyOn(backendApi, 'sessionPlan').mockRejectedValue(new Error('plan endpoint unavailable'));
    vi.spyOn(backendApi, 'sessionTodos').mockResolvedValue({ todos: [] });
  });

  afterEach(() => {
    state.socket?.dispose();
    stopStreamWatchdog();
    authStore.set({ isLoggedIn: false });
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  function disconnectAndReconnect(): void {
    emitSocket({ type: 'close', code: 1012 });
    expect(sessionStore.get().busySessions[sid]).toBe(true);
    expect(sessionStore.get().books[sid]?.turnSealed).toBe(false);
    expect(messageStore.get().messages[sid][0].streaming).toBe(true);
    emitSocket({ type: 'open' });
  }

  it.each([true, false])('recovers durable history when the interrupted request finished offline (foreground=%s)', async (active) => {
    if (!active) setActiveSessionId(null);
    const status = vi.spyOn(backendApi, 'sessionStatus').mockResolvedValue(settledStatus);
    const history = vi.spyOn(backendApi, 'history').mockResolvedValue([
      { role: 'user', content: 'Prompt' },
      { role: 'assistant', content: 'Completed while offline' },
    ]);

    disconnectAndReconnect();
    await vi.advanceTimersByTimeAsync(0);

    expect(status.mock.invocationCallOrder[0]).toBeLessThan(history.mock.invocationCallOrder[0]);
    expect(messageStore.get().messages[sid].map((message) => message.content)).toEqual([
      'Prompt', 'Completed while offline',
    ]);
    expect(sessionStore.get().books[sid]?.turnSealed).toBe(true);
    expect(sessionStore.get().busySessions[sid]).toBeFalsy();
  });

  it('does not treat a status transport error as a completed turn', async () => {
    vi.spyOn(backendApi, 'sessionStatus').mockRejectedValue(new Error('connection unavailable'));
    disconnectAndReconnect();
    await vi.advanceTimersByTimeAsync(0);
    expect(sessionStore.get().books[sid]?.activeRequestId).toBe(oldRequest);
    expect(sessionStore.get().busySessions[sid]).toBe(true);
    expect(messageStore.get().messages[sid][0].streaming).toBe(true);
  });

  it('discards a reconnect status response after a new request starts', async () => {
    const delayed = Promise.withResolvers<typeof settledStatus>();
    vi.spyOn(backendApi, 'sessionStatus').mockReturnValue(delayed.promise);
    const history = vi.spyOn(backendApi, 'history').mockResolvedValue([]);
    disconnectAndReconnect();
    openTurnForRequest(sid, 'req-new');
    delayed.resolve(settledStatus);
    await vi.advanceTimersByTimeAsync(0);

    expect(history).not.toHaveBeenCalled();
    expect(sessionStore.get().books[sid]?.activeRequestId).toBe('req-new');
    expect(sessionStore.get().books[sid]?.turnSealed).toBe(false);
  });

  it('keeps a user-stopped request closed when a reconnect status response arrives', async () => {
    const delayed = Promise.withResolvers<typeof settledStatus>();
    vi.spyOn(backendApi, 'sessionStatus').mockReturnValue(delayed.promise);
    disconnectAndReconnect();
    stopGeneration(sid);
    delayed.resolve({ ...settledStatus, live: 'running' });
    await vi.advanceTimersByTimeAsync(0);

    expect(sessionStore.get().books[sid]?.turnSealed).toBe(true);
    expect(sessionStore.get().busySessions[sid]).toBeFalsy();
    expect(messageStore.get().messages[sid][0].interrupted).toBe(true);
  });
});

describe('shouldSkipHistoryReloadOnReconnect', () => {
  it('renders each external session with its provider initial and stable pattern', () => {
    configStore.set({
      config: { external_agents: { enabled: true } } as never,
    });
    sessionStore.set({
      sessions: [
        {
          id: 'sid-kimi', title: 'Kimi session', updatedAt: 2, preview: '', badge: '', workspaceId: 'default',
          agentLabel: { provider: 'kimi', display_badge: 'K', name: 'Kimi' },
          agentBinding: { kind: 'external_agent', id: 'agent-kimi' },
        },
        {
          id: 'sid-codex', title: 'Codex session', updatedAt: 1, preview: '', badge: '', workspaceId: 'default',
          agentLabel: { provider: 'codex', display_badge: 'X', name: 'Codex' },
          agentBinding: { kind: 'external_agent', id: 'agent-codex' },
        },
      ],
    });
    const host = document.createElement('div');
    document.body.append(host);
    const view = createSessionHistoryView(host, {
      openSession: vi.fn(),
      createSession: vi.fn(),
      createWorkspace: vi.fn(),
      manageHistory: vi.fn(),
      openWorkspace: vi.fn(),
      refreshSessions: async () => undefined,
      retrySessions: async () => undefined,
      retryWorkspaces: async () => undefined,
      getLoadErrors: () => ({ sessions: null, workspaces: null }),
    });

    const kimi = host.querySelector('[data-session-open="sid-kimi"] [data-session-identity-icon]');
    const codex = host.querySelector('[data-session-open="sid-codex"] [data-session-identity-icon]');
    expect(kimi?.textContent).toBe('K');
    expect(kimi?.classList.contains('agent-provider-tone-0')).toBe(true);
    expect(codex?.textContent).toBe('X');
    expect(codex?.classList.contains('agent-provider-tone-1')).toBe(true);
    expect(host.querySelector('[data-session-identity-icon] image')).toBeNull();

    view.dispose();
  });

  it('renders Team sessions with the shared black-white dual Agent logo', () => {
    configStore.set({
      config: { external_agents: { enabled: true } } as never,
    });
    sessionStore.set({
      sessions: [{
        id: 'sid-team', title: 'Team session', updatedAt: 1, preview: '', badge: '', workspaceId: 'default',
        agentLabel: { provider: 'team', display_badge: 'T', name: '研发团队' },
        agentBinding: { kind: 'external_team', id: 'team-dev' },
      }],
    });
    const host = document.createElement('div');
    document.body.append(host);
    const view = createSessionHistoryView(host, {
      openSession: vi.fn(),
      createSession: vi.fn(),
      createWorkspace: vi.fn(),
      manageHistory: vi.fn(),
      openWorkspace: vi.fn(),
      refreshSessions: async () => undefined,
      retrySessions: async () => undefined,
      retryWorkspaces: async () => undefined,
      getLoadErrors: () => ({ sessions: null, workspaces: null }),
    });

    const identityIcon = host.querySelector('[data-session-open="sid-team"] [data-session-identity-icon]');
    expect(identityIcon?.querySelector('.session__team-logo')).not.toBeNull();
    expect(identityIcon?.querySelectorAll('.session__team-logo i')).toHaveLength(2);
    expect(identityIcon?.querySelector('svg')).toBeNull();

    view.dispose();
  });

  it('returns true when session is busy with an active request', () => {
    ensureSessionBook('sid-1');
    patchBook('sid-1', { activeRequestId: 'req-1', turnSealed: false, acceptingNewRequest: false });
    setBusy('sid-1', true);
    expect(shouldSkipHistoryReloadOnReconnect('sid-1')).toBe(true);
  });

  it('returns true when assistant message is still streaming', () => {
    messageStore.set({
      messages: {
        'sid-1': [{
          id: 'm-1',
          role: 'assistant',
          content: 'partial',
          timestamp: Date.now(),
          streaming: true,
        }],
      },
    });
    expect(shouldSkipHistoryReloadOnReconnect('sid-1')).toBe(true);
  });

  it('returns false for idle session with sealed turn', () => {
    ensureSessionBook('sid-1');
    patchBook('sid-1', { turnSealed: true, acceptingNewRequest: false, activeRequestId: null });
    expect(shouldSkipHistoryReloadOnReconnect('sid-1')).toBe(false);
  });
});
