/**
 * @vitest-environment happy-dom
 *
 * 渠道会话 channel_session_updated 的用户消息补插去重：
 * 桌面本地发送时尾部是乐观 assistant 占位，去重必须向前找最后一条 user 消息，
 * 否则会重复插入一条相同的用户气泡。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  _resetTurnDurationTickerForTests,
  applyChunk,
} from '../../src/ui/features/chat-controller';
import { clearFoldMemoryCache } from '../../src/ui/features/fold-state';
import { __resetAllStoresForTest, messageStore } from '../../src/ui/stores/stores';
import { appendSessionMessage, setActiveSessionId } from '../../src/ui/state';
import type { ChatChunk } from '../../src/ui/backend-client';

vi.mock('../../src/ui/features/workspaces', () => ({
  refreshAllSessions: vi.fn(async () => undefined),
  renderWorkspaceHistory: vi.fn(),
  commitDraftSession: vi.fn(),
  createSessionInWorkspace: vi.fn(() => 'sid-1'),
  isDraftSession: vi.fn(() => false),
  getSessionAgentDisplay: vi.fn(() => null),
}));

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
  resetPlanBoardDraft: vi.fn(),
  invalidateFileDiffCachePaths: vi.fn(),
  setUsageSnapshot: vi.fn(),
  revealPathInFolder: vi.fn(),
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

const SID = 'agent:main:weixin:dm:u1';

function channelUpdatedChunk(query: string): ChatChunk {
  return {
    kind: 'channel_session_updated',
    body: { platform: 'weixin', event: 'agent:start', query },
    is_final: false,
    sequence: 1,
    request_id: 'req-1',
    session_id: SID,
  };
}

function userMessages() {
  return (messageStore.get().messages[SID] ?? []).filter((m) => m.role === 'user');
}

beforeEach(() => {
  vi.clearAllMocks();
  __resetAllStoresForTest();
  clearFoldMemoryCache();
  window.localStorage.clear();
  setActiveSessionId(SID);
  document.body.innerHTML = `
    <div id="welcome-panel"></div>
    <div id="chat-panel" hidden><div id="chat-messages"></div></div>
    <div id="chat-todo-slot"></div>
    <div id="composer-controls"></div>
    <div id="chat-running-intro"></div>
  `;
});

afterEach(() => {
  _resetTurnDurationTickerForTests();
});

describe('channel_session_updated 用户消息补插去重', () => {
  it('桌面本地发送后（尾部为乐观 assistant 占位）不重复补插用户消息', () => {
    // 模拟 dispatchWs 后的本地状态：乐观 user 气泡 + 乐观 assistant 占位
    appendSessionMessage(SID, {
      id: 'u-1',
      role: 'user',
      content: 'hhh',
      timestamp: Date.now(),
    });
    appendSessionMessage(SID, {
      id: 'a-1',
      role: 'assistant',
      content: '',
      timestamp: Date.now(),
      streaming: true,
    });

    applyChunk(channelUpdatedChunk('hhh'));

    expect(userMessages()).toHaveLength(1);
    expect(userMessages()[0]!.content).toBe('hhh');
  });

  it('渠道外部发来的消息（本地无乐观气泡）正常补插', () => {
    applyChunk(channelUpdatedChunk('微信端消息'));

    expect(userMessages()).toHaveLength(1);
    expect(userMessages()[0]!.content).toBe('微信端消息');
  });

  it('本地最后一条 user 消息内容不同（新消息）时正常补插', () => {
    appendSessionMessage(SID, {
      id: 'u-1',
      role: 'user',
      content: '上一轮消息',
      timestamp: Date.now(),
    });
    appendSessionMessage(SID, {
      id: 'a-1',
      role: 'assistant',
      content: '上一轮回复',
      timestamp: Date.now(),
    });

    applyChunk(channelUpdatedChunk('微信端新消息'));

    expect(userMessages()).toHaveLength(2);
    expect(userMessages()[1]!.content).toBe('微信端新消息');
  });
});
