/**
 * @vitest-environment happy-dom
 *
 * Wiki reducer 宿主生命周期测试：验证 init/dispose 与 capability 切换的实际接线。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ChatChunk } from '../../src/ui/backend-client';
import {
  applyChunk,
  setWikiIngestProgressCallback,
} from '../../src/ui/features/chat-controller';
import {
  __resetWikiAgentForTest,
  disposeWikiAgentFeature,
  initWikiAgent,
} from '../../src/ui/features/wiki-agent';
import { featureEventRegistry } from '../../src/ui/features/event-reducer-registry';
import {
  normalizeChunk,
  reduceChunk,
  type ReducerSnapshot,
} from '../../src/ui/reducers/chat-reducer';
import {
  __resetAllStoresForTest,
  configStore,
  messageStore,
  sessionStore,
} from '../../src/ui/stores/stores';

vi.mock('../../src/ui/features/running-intro', () => ({ syncRunningIntroSlot: vi.fn() }));
vi.mock('../../src/ui/features/usage-tracker', () => ({ recordTurn: vi.fn() }));
vi.mock('../../src/ui/features/cron-page', () => ({ onAfterFinal: vi.fn() }));
vi.mock('../../src/ui/features/kanban-board', () => ({
  refreshKanbanBoard: vi.fn(async () => undefined),
  renderKanbanBoard: vi.fn(),
}));
vi.mock('../../src/ui/features/inspector', () => ({
  isInspectorOpen: vi.fn(() => false),
  openInspectorToTab: vi.fn(),
  refreshInspector: vi.fn(),
  refreshInspectorChrome: vi.fn(),
}));
vi.mock('../../src/ui/features/composer-toolbar', () => ({
  syncComposerModelLabel: vi.fn(),
  syncComposerWorkspaceLabel: vi.fn(),
}));
vi.mock('../../src/ui/features/model-picker', () => ({ syncModelUi: vi.fn() }));
vi.mock('../../src/ui/features/system-page', () => ({ renderSystemOverview: vi.fn() }));
vi.mock('../../src/ui/features/attachments', () => ({
  takeAttachmentsForSend: vi.fn(() => []),
  renderAttachmentPreview: vi.fn(),
  renderAttachmentList: vi.fn(),
  bindFilePaste: vi.fn(),
  bindFileDrop: vi.fn(),
}));
vi.mock('../../src/ui/features/session-model', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../src/ui/features/session-model')>()),
  persistDraftSessionModel: vi.fn(async () => undefined),
}));

function snapshot(messages: ReducerSnapshot['messages'] = []): ReducerSnapshot {
  return {
    sessionId: 'wiki-test-session',
    messages,
    book: {
      toolMap: new Map(),
      assistantId: null,
      firstChunkAt: null,
      planActive: false,
      pendingPlan: null,
      pendingFollowup: null,
      todos: [],
      fileChanges: [],
      prevTurnFileSignature: null,
      deltaSpans: [],
      legacyDeltaText: '',
      turnSealed: false,
      activeRequestId: null,
      acceptingNewRequest: false,
    },
    currentStatus: 'idle',
    now: 1_700_000_000_000,
    sequence: 1,
  };
}

function wikiConfig(enabled: boolean) {
  return { wiki: { enabled } } as Parameters<typeof configStore.set>[0]['config'];
}

function wikiCardsChunk(): ChatChunk {
  return {
    kind: 'wiki_cards',
    body: { pages: [{ id: 'p1', title: 'Wiki page' }] },
    session_id: 'wiki-test-session',
    sequence: 1,
    is_final: false,
  } as ChatChunk;
}

beforeEach(() => {
  disposeWikiAgentFeature();
  __resetWikiAgentForTest();
  __resetAllStoresForTest();
  document.body.innerHTML = '';
  sessionStore.set({ activeSessionId: 'wiki-test-session' });
  vi.restoreAllMocks();
});

afterEach(() => {
  setWikiIngestProgressCallback(null);
  disposeWikiAgentFeature();
});

describe('Desktop Wiki reducer host lifecycle', () => {
  it('starts disabled with no Wiki handlers, then enables and disables atomically', () => {
    configStore.set({ config: wikiConfig(false) });
    initWikiAgent();
    expect(reduceChunk(normalizeChunk(wikiCardsChunk())!, snapshot()).messageUpserts).toEqual([]);

    configStore.set({ config: wikiConfig(true) });
    window.dispatchEvent(new CustomEvent('wiki:config-change'));
    expect(reduceChunk(normalizeChunk(wikiCardsChunk())!, snapshot()).messageUpserts[0]?.op).toBe('append');

    configStore.set({ config: wikiConfig(false) });
    window.dispatchEvent(new CustomEvent('wiki:config-change'));
    expect(reduceChunk(normalizeChunk(wikiCardsChunk())!, snapshot()).messageUpserts).toEqual([]);
  });

  it('repeated init, config events, dispose and re-init do not duplicate registrations', () => {
    configStore.set({ config: wikiConfig(true) });
    initWikiAgent();
    initWikiAgent();
    window.dispatchEvent(new CustomEvent('wiki:config-change'));
    expect(() => initWikiAgent()).not.toThrow();

    const first = reduceChunk(normalizeChunk(wikiCardsChunk())!, snapshot());
    expect(first.messageUpserts).toHaveLength(1);

    disposeWikiAgentFeature();
    disposeWikiAgentFeature();
    expect(reduceChunk(normalizeChunk(wikiCardsChunk())!, snapshot()).messageUpserts).toEqual([]);

    initWikiAgent();
    const second = reduceChunk(normalizeChunk(wikiCardsChunk())!, snapshot());
    expect(second.messageUpserts).toHaveLength(1);
  });

  it('drops cards and ingest progress while disabled without message, progress, or DOM side effects', () => {
    configStore.set({ config: wikiConfig(false) });
    initWikiAgent();
    const progress = vi.fn();
    setWikiIngestProgressCallback(progress);
    const eventSpy = vi.spyOn(window, 'dispatchEvent');
    const before = document.body.innerHTML;

    applyChunk({
      kind: 'wiki_ingest_progress',
      body: { stage: 'compile', percent: 50, source_id: 'src-1' },
      session_id: 'wiki-test-session', sequence: 2, is_final: false,
    } as ChatChunk);
    applyChunk({ ...wikiCardsChunk(), sequence: 3 });

    expect(progress).not.toHaveBeenCalled();
    expect(messageStore.get().messages).toEqual({});
    expect(document.body.innerHTML).toBe(before);
    expect(eventSpy.mock.calls.some(([event]) => event.type === 'messages:changed')).toBe(false);
  });

  it('processes each Wiki frame while enabled, then stops processing the same frame kinds after disable', () => {
    const progress = vi.fn();
    setWikiIngestProgressCallback(progress);
    configStore.set({ config: wikiConfig(true) });
    initWikiAgent();
    applyChunk({
      kind: 'wiki_ingest_progress', body: { stage: 'compile', percent: 50, source_id: 'src-1' },
      session_id: 'wiki-test-session', sequence: 10, is_final: false,
    } as ChatChunk);
    applyChunk({ ...wikiCardsChunk(), sequence: 11 });
    expect(progress).toHaveBeenCalledTimes(1);
    expect(Object.values(messageStore.get().messages).flat()).toHaveLength(1);

    configStore.set({ config: wikiConfig(false) });
    window.dispatchEvent(new CustomEvent('wiki:config-change'));
    const beforeMessages = messageStore.get().messages;
    applyChunk({
      kind: 'wiki_ingest_progress', body: { stage: 'disabled', percent: 90, source_id: 'src-2' },
      session_id: 'wiki-test-session', sequence: 12, is_final: false,
    } as ChatChunk);
    applyChunk({ ...wikiCardsChunk(), sequence: 13 });
    expect(progress).toHaveBeenCalledTimes(1);
    expect(messageStore.get().messages).toBe(beforeMessages);
  });

  it('rolls back earlier registrations when a later Wiki registration conflicts', () => {
    const disposeConflict = featureEventRegistry.register({
      feature: 'wiki', event: 'ingest_progress', version: 1,
      reducer: () => ({ messageUpserts: [], toolUpserts: [], replaceBook: null, statusHint: undefined, queueHint: undefined, finalize: false }),
    });
    configStore.set({ config: wikiConfig(true) });
    expect(() => initWikiAgent()).toThrow('already registered');
    expect(featureEventRegistry.dispatch('wiki', 'cards', 1, {}, snapshot())).toBeNull();
    disposeConflict();
  });

  it('routes wiki_changed through the installed Wiki feature handler while enabled', () => {
    configStore.set({ config: wikiConfig(true) });
    initWikiAgent();
    const wikiChange = vi.fn();
    window.addEventListener('wiki:changed', wikiChange);

    applyChunk({
      kind: 'wiki_changed', body: { changes: [{ id: 'p1' }] },
      session_id: 'wiki-test-session', sequence: 3, is_final: false,
    } as ChatChunk);

    expect(wikiChange).toHaveBeenCalledTimes(1);
    expect((wikiChange.mock.calls[0]?.[0] as CustomEvent).detail).toMatchObject({
      sessionId: 'wiki-test-session', changes: [{ id: 'p1' }],
    });
    window.removeEventListener('wiki:changed', wikiChange);
  });

  it('keeps team events available when Wiki is disabled and leaves Wiki page event wiring observable', () => {
    const disposeTeam = featureEventRegistry.register({
      feature: 'team', event: 'internal_message', version: 1,
      reducer: () => ({
        messageUpserts: [{ op: 'append', message: { id: 'team-1', role: 'team_internal', content: 'team' } }],
        toolUpserts: [], replaceBook: null, statusHint: 'running', queueHint: undefined, finalize: false,
      }),
    });
    configStore.set({ config: wikiConfig(false) });
    initWikiAgent();
    const team = reduceChunk({
      kind: 'team_internal', body: { text: 'team' }, sequence: 1,
    }, snapshot());
    expect(team.messageUpserts[0]?.message?.role).toBe('team_internal');

    const wikiChange = vi.fn();
    window.addEventListener('wiki:changed', wikiChange);
    applyChunk({
      kind: 'wiki_changed', body: { changes: [{ id: 'p1' }] },
      session_id: 'wiki-test-session', sequence: 3, is_final: false,
    } as ChatChunk);
    expect(wikiChange).not.toHaveBeenCalled();
    window.removeEventListener('wiki:changed', wikiChange);
    disposeTeam();
  });
});
