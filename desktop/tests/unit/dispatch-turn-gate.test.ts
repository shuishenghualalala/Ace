/**
 * @vitest-environment happy-dom
 *
 * dispatch 开回合与 applyChunk gate：首帧必须在 turnSealed=false + activeRequestId 绑定后到达。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { applyChunk } from '../../src/ui/features/chat-controller';
import { openTurnForRequest } from '../../src/ui/features/session-busy';
import { resolveTurnGate } from '../../src/ui/reducers/chat-reducer';
import { __resetAllStoresForTest, messageStore, sessionStore } from '../../src/ui/stores/stores';
import { appendSessionMessage, setActiveExternalTeamForSession, setActiveSessionId } from '../../src/ui/state';
import type { ChatChunk } from '../../src/ui/backend-client';

vi.mock('../../src/ui/features/workspaces', () => ({
  refreshAllSessions: vi.fn(async () => undefined),
  renderWorkspaceHistory: vi.fn(),
  commitDraftSession: vi.fn(),
  createSessionInWorkspace: vi.fn(() => 'sid-1'),
  getSessionAgentDisplay: vi.fn(() => null),
  isDraftSession: vi.fn(() => false),
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
}));
vi.mock('../../src/ui/features/composer-toolbar', () => ({
  syncComposerModelLabel: vi.fn(),
}));
vi.mock('../../src/ui/features/model-picker', () => ({ syncModelUi: vi.fn() }));
vi.mock('../../src/ui/features/system-page', () => ({ renderSystemOverview: vi.fn() }));
vi.mock('../../src/ui/features/attachments', () => ({
  takeAttachmentsForSend: vi.fn(() => []),
  renderAttachmentPreview: vi.fn(),
}));

function chunk(kind: ChatChunk['kind'], requestId: string, body: Record<string, unknown> = {}): ChatChunk {
  return {
    kind,
    body,
    is_final: false,
    sequence: 1,
    request_id: requestId,
    session_id: 'sid-1',
  };
}

beforeEach(() => {
  __resetAllStoresForTest();
  setActiveSessionId('sid-1');
  document.body.innerHTML = '<div id="chat-messages"></div><div id="composer-controls"></div><div id="chat-running-intro"></div>';
});

describe('dispatch turn gate', () => {
  it('drops scoped frames on default sealed book before turn is opened', () => {
    expect(resolveTurnGate('task', 'req-1', {
      turnSealed: true,
      activeRequestId: null,
      acceptingNewRequest: false,
    })).toEqual({ action: 'drop' });

    applyChunk(chunk('delta', 'req-1', { text: 'hi' }));
    expect(messageStore.get().messages['sid-1'] ?? []).toEqual([]);
  });

  it('accepts delta/task after openTurnForRequest', () => {
    openTurnForRequest('sid-1', 'req-1');

    applyChunk(chunk('task', 'req-1', { task_id: 't1' }));
    applyChunk(chunk('delta', 'req-1', { text: '你好' }));

    const msgs = messageStore.get().messages['sid-1'] ?? [];
    expect(msgs.length).toBe(1);
    expect(msgs[0]?.role).toBe('assistant');
    expect(msgs[0]?.content).toBe('你好');
    expect(sessionStore.get().books['sid-1']?.activeRequestId).toBe('req-1');
  });

  it('keeps Team messages in planning-to-summary order and suppresses the root final bubble', () => {
    appendSessionMessage('sid-1', {
      id: 'user-team',
      role: 'user',
      content: '开发贪吃蛇',
      timestamp: 900,
    });
    openTurnForRequest('sid-1', 'req-team');

    applyChunk(chunk('team_internal', 'req-team', {
      text: '收到，我会先规划 DAG。',
      agent_id: 'crew::builtin',
      agent_name: 'Crew',
      is_leader: true,
      event_type: 'team_decision',
      node_id: 'leader_plan',
    }));
    applyChunk({
      ...chunk('team_internal', 'req-team', {
        text: '@hermes 开始实现贪吃蛇。',
        agent_id: 'crew::builtin',
        agent_name: 'Crew',
        is_leader: true,
        event_type: 'team_assign',
        node_id: 'build',
      }),
      sequence: 2,
    });
    applyChunk({
      ...chunk('team_internal', 'req-team', {
        text: '实现与测试已经完成。',
        agent_id: 'crew::builtin',
        agent_name: 'Crew',
        is_leader: true,
        event_type: 'team_summary',
        node_id: 'leader_summary',
      }),
      sequence: 3,
    });
    applyChunk({ ...chunk('final', 'req-team', { text: '实现与测试已经完成。' }), sequence: 4 });

    const messages = messageStore.get().messages['sid-1'] ?? [];
    expect(messages.map((message) => message.role)).toEqual([
      'user',
      'team_internal',
      'team_internal',
      'team_internal',
    ]);
    expect(messages.map((message) => message.content)).toEqual([
      '开发贪吃蛇',
      '收到，我会先规划 DAG。',
      '@hermes 开始实现贪吃蛇。',
      '实现与测试已经完成。',
    ]);
    expect(sessionStore.get().books['sid-1']?.hadTeamInternal).toBe(false);
  });

  it('keeps Team transport status ephemeral instead of creating an assistant bubble', () => {
    setActiveExternalTeamForSession('sid-1', 'team-snake');
    openTurnForRequest('sid-1', 'req-team-status');

    applyChunk(chunk('status', 'req-team-status', {
      message: '正在更新 DAG 节点状态',
    }));
    applyChunk({
      ...chunk('team_internal', 'req-team-status', {
        text: '正在实现游戏逻辑',
        process_text: '已完成数据结构设计',
        agent_id: 'hermes',
        agent_name: 'Hermes',
        event_type: 'team_stream',
        node_id: 'build',
        display_mode: 'stream',
      }),
      sequence: 2,
    });

    const messages = messageStore.get().messages['sid-1'] ?? [];
    expect(messages).toHaveLength(1);
    expect(messages[0]?.role).toBe('team_internal');
    expect(messages[0]?.content).toBe('正在实现游戏逻辑');
    expect(messages.some((message) => message.content.includes('更新 DAG'))).toBe(false);
  });

  it('reuses optimistic assistant and preserves turnStartedAt across first delta', () => {
    const t0 = 1_700_000_000_000;
    vi.spyOn(Date, 'now').mockReturnValue(t0);
    openTurnForRequest('sid-1', 'req-1');
    const book = sessionStore.get().books['sid-1'];
    const optimisticId = book?.assistantId;
    expect(optimisticId).toBeTruthy();
    expect(messageStore.get().messages['sid-1']?.[0]?.turnStartedAt).toBe(t0);
    expect(messageStore.get().messages['sid-1']?.[0]?.segmentRole).toBe('process');

    vi.spyOn(Date, 'now').mockReturnValue(t0 + 90_000);
    applyChunk(chunk('delta', 'req-1', { text: '开始写代码' }));

    const msgs = messageStore.get().messages['sid-1'] ?? [];
    expect(msgs).toHaveLength(1);
    expect(msgs[0]?.id).toBe(optimisticId);
    expect(msgs[0]?.content).toBe('开始写代码');
    expect(msgs[0]?.turnStartedAt).toBe(t0);
    expect(msgs[0]?.segmentRole).toBe('process');
    expect(sessionStore.get().books['sid-1']?.assistantId).toBe(optimisticId);
    expect(sessionStore.get().books['sid-1']?.firstChunkAt).toBe(t0 + 90_000);
    vi.restoreAllMocks();
  });

  it('keeps approved pendingPlan after approve-resume delta for Plan Board', () => {
    openTurnForRequest('sid-1', 'req-plan');
    sessionStore.get().books['sid-1']!.pendingPlan = {
      plan: '# 方案\n\n- step',
      planFile: 'plans/p.md',
      status: 'approved',
    };
    // 批准路径走 resume，不重新 openTurn；此处模拟执行首包 delta
    applyChunk(chunk('delta', 'req-plan', { text: '按方案执行' }));
    expect(sessionStore.get().books['sid-1']?.pendingPlan).toEqual({
      plan: '# 方案\n\n- step',
      planFile: 'plans/p.md',
      status: 'approved',
    });
  });

  it('first tool chunk patches optimistic assistant instead of appending a second bubble', () => {
    openTurnForRequest('sid-1', 'req-1');
    const optimisticId = sessionStore.get().books['sid-1']?.assistantId;
    const startedAt = messageStore.get().messages['sid-1']?.[0]?.turnStartedAt;

    applyChunk(chunk('tool', 'req-1', {
      tool_call_id: 't1',
      phase: 'start',
      name: 'file_write',
      ui_label: '写入 demo.html',
    }));

    const msgs = messageStore.get().messages['sid-1'] ?? [];
    expect(msgs).toHaveLength(1);
    expect(msgs[0]?.id).toBe(optimisticId);
    expect(msgs[0]?.turnStartedAt).toBe(startedAt);
    expect(msgs[0]?.toolCalls?.length).toBe(1);
    expect(msgs[0]?.toolCalls?.[0]?.name).toBe('file_write');
  });
});
