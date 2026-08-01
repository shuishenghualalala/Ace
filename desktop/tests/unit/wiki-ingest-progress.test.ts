/**
 * @vitest-environment happy-dom
 *
 * wiki_ingest_progress 帧分发单测：
 * applyChunk 收到该帧后规范化 body（percent 钳制 / label 回落 / error 透传）并
 * 转发给 setWikiIngestProgressCallback 注册的订阅者；不进 reducer、不写消息。
 * mock 清单与 chat-controller-send.test.ts 一致。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { applyChunk, setWikiIngestProgressCallback } from '../../src/ui/features/chat-controller';
import type { WikiIngestProgress } from '../../src/ui/backend-client';
import { __resetAllStoresForTest, messageStore, sessionStore } from '../../src/ui/stores/stores';

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
}));
vi.mock('../../src/ui/features/session-model', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../src/ui/features/session-model')>();
  return {
    ...actual,
    persistDraftSessionModel: vi.fn(async () => undefined),
  };
});
function progressChunk(body: Record<string, unknown>, sessionId = 'sid-1') {
  return {
    kind: 'wiki_ingest_progress' as const,
    body,
    is_final: false,
    sequence: 0,
    session_id: sessionId,
  };
}

beforeEach(() => {
  __resetAllStoresForTest();
  sessionStore.set({ activeSessionId: 'sid-1' });
  setWikiIngestProgressCallback(null);
  document.body.innerHTML = '';
});

describe('wiki_ingest_progress 帧分发', () => {
  it('规范化 body 后转发给注册回调，且不写对话消息', () => {
    const cb = vi.fn();
    setWikiIngestProgressCallback(cb);

    applyChunk(progressChunk({ stage: 'analyze', percent: 130, source_id: 's1' }));

    expect(cb).toHaveBeenCalledTimes(1);
    const progress = cb.mock.calls[0][0] as WikiIngestProgress;
    // percent 钳制到 100；label 缺省回落 stage；session_id 取自帧
    expect(progress).toMatchObject({
      stage: 'analyze',
      percent: 100,
      label: 'analyze',
      source_id: 's1',
      session_id: 'sid-1',
    });
    expect(progress.error).toBeUndefined();
    // 带外帧不进 reducer：不产生任何对话消息
    expect(messageStore.get().messages).toEqual({});
  });

  it('label / error / detail 透传，percent 下限钳制为 0', () => {
    const cb = vi.fn();
    setWikiIngestProgressCallback(cb);

    applyChunk(
      progressChunk({
        stage: 'done',
        percent: -5,
        label: '编译完成',
        source_id: 's2',
        error: 'LLM 分析失败',
        detail: { label: '编译完成' },
      }),
    );

    expect(cb).toHaveBeenCalledTimes(1);
    expect(cb.mock.calls[0][0]).toMatchObject({
      stage: 'done',
      percent: 0,
      label: '编译完成',
      source_id: 's2',
      error: 'LLM 分析失败',
      detail: { label: '编译完成' },
    });
  });

  it('未注册回调时不抛错、静默丢弃', () => {
    expect(() => applyChunk(progressChunk({ stage: 'load', percent: 10, source_id: 's1' }))).not.toThrow();
    expect(messageStore.get().messages).toEqual({});
  });
});
