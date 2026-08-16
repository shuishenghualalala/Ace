/**
 * @vitest-environment node
 */
import { describe, expect, it, vi } from 'vitest';

vi.mock('../../src/ui/state', () => ({
  state: { configModel: 'test-model' },
  newMessageId: (role: string) => `${role}-id`,
}));

vi.mock('../../src/ui/features/session-model', () => ({
  sessionDisplayModelLabel: () => 'test-model',
  sessionMessageModelLabel: (_sessionId: string | null | undefined, model: string) => model,
}));

import {
  filterExistingTurnFileChanges,
  inferTurnFileChangesFromToolCalls,
  mapBackendHistoryItem,
  mergeTeamInternalMessage,
  mergeTeamInternalMessages,
} from '../../src/ui/features/history-mapping';
import type { BackendHistoryItem } from '../../src/ui/backend-client';
import type { TurnFileChangeSummary } from '../../src/ui/chat-render';

describe('mapBackendHistoryItem turnFileChanges', () => {
  it('历史消息优先保留生成当时的模型', () => {
    const msg = mapBackendHistoryItem({
      role: 'assistant',
      content: 'answer',
      model: 'model-at-turn',
    });

    expect(msg.model).toBe('model-at-turn');
  });

  it('maps turn_file_changes from history API onto the message', () => {
    const item: BackendHistoryItem = {
      role: 'assistant',
      content: '写好了',
      turn_file_changes: [
        { path: 'C:\\Users\\x\\Desktop\\snake_game.html', name: 'snake_game.html', added: 419, removed: 117, status: 'modified' },
      ],
    };
    const msg = mapBackendHistoryItem(item);
    expect(msg.turnFileChanges).toEqual([
      { path: 'C:\\Users\\x\\Desktop\\snake_game.html', name: 'snake_game.html', added: 419, removed: 117, status: 'modified' },
    ]);
    expect(msg.turnFileChangesPersistedPaths).toEqual(['C:\\Users\\x\\Desktop\\snake_game.html']);
  });

  it('filters metadata-only empty added files from persisted history like the live reducer', () => {
    const msg = mapBackendHistoryItem({
      role: 'assistant',
      content: 'done',
      turn_file_changes: [
        { path: '/work/.turn-marker', name: '.turn-marker', added: 0, removed: 0, status: 'added' },
        { path: '/work/result.txt', name: 'result.txt', added: 1, removed: 0, status: 'added' },
      ],
    });

    expect(msg.turnFileChanges?.map((file) => file.path)).toEqual(['/work/result.txt']);
    expect(msg.turnFileChangesPersistedPaths).toEqual(['/work/result.txt']);
  });

  it('maps Team member turn_file_changes without changing artifact data', () => {
    const item: BackendHistoryItem = {
      role: 'team_internal',
      content: '成员已提交',
      agent_id: 'hermes',
      event_type: 'team_submit',
      artifacts: [{ title: 'result.md', path: '/work/result.md', kind: 'text' }],
      turn_file_changes: [
        { path: '/work/result.md', name: 'result.md', added: 8, removed: 1, status: 'modified' },
      ],
    };

    const msg = mapBackendHistoryItem(item);

    expect(msg.turnFileChanges).toEqual([
      { path: '/work/result.md', name: 'result.md', added: 8, removed: 1, status: 'modified' },
    ]);
    expect(msg.artifacts).toEqual(item.artifacts);
  });

  it('falls back to file_write tool paths when turn_file_changes is absent (legacy history)', () => {
    const item: BackendHistoryItem = {
      role: 'assistant',
      content: '写好了',
      tool_calls: [
        {
          id: 'tc1',
          name: 'file_write',
          arguments: { path: 'C:\\Users\\x\\Desktop\\snake_game.html', content: '<html></html>' },
        },
      ],
    };
    const msg = mapBackendHistoryItem(item);
    expect(msg.turnFileChanges).toEqual([
      {
        path: 'C:\\Users\\x\\Desktop\\snake_game.html',
        name: 'snake_game.html',
        added: 0,
        removed: 0,
        status: 'modified',
      },
    ]);
  });

  it('prefers persisted turn_file_changes over tool_call inference', () => {
    const item: BackendHistoryItem = {
      role: 'assistant',
      content: '写好了',
      turn_file_changes: [
        { path: 'a.html', name: 'a.html', added: 10, removed: 2, status: 'modified' },
      ],
      tool_calls: [
        { id: 'tc1', name: 'file_write', arguments: { path: 'b.html', content: 'x' } },
      ],
    };
    const msg = mapBackendHistoryItem(item);
    expect(msg.turnFileChanges?.[0]?.path).toBe('a.html');
    expect(msg.turnFileChanges?.[0]?.added).toBe(10);
  });

  it('preserves binary result metadata from history', () => {
    const item: BackendHistoryItem = {
      role: 'assistant',
      content: 'PPT 已生成',
      turn_file_changes: [
        { path: '/work/final.pptx', name: 'final.pptx', added: 0, removed: 0, status: 'added', binary: true },
      ],
    };
    expect(mapBackendHistoryItem(item).turnFileChanges).toEqual([{
      path: '/work/final.pptx',
      name: 'final.pptx',
      added: 0,
      removed: 0,
      status: 'added',
      binary: true,
    }]);
  });

  it('merges an old terminal-generated PPTX into the persisted process-file list', () => {
    const output = '/Users/test/task_workspaces/default/产品招聘看板平台介绍.pptx';
    const item: BackendHistoryItem = {
      role: 'assistant',
      content: 'PPT 已生成',
      turn_file_changes: [
        { path: '/Users/test/task_workspaces/default/ppt/slide01.svg', name: 'slide01.svg', added: 12, removed: 0, status: 'added' },
      ],
      tool_calls: [
        {
          id: 'terminal-build',
          name: 'terminal',
          arguments: { command: `node build.js --output "${output}"` },
          result: JSON.stringify({
            success: true,
            exit_code: 0,
            command: `node build.js --output "${output}"`,
            output: `Done: ${output}\n`,
          }),
        },
      ],
    };
    expect(mapBackendHistoryItem(item).turnFileChanges).toEqual([
      { path: '/Users/test/task_workspaces/default/ppt/slide01.svg', name: 'slide01.svg', added: 12, removed: 0, status: 'added' },
      { path: output, name: '产品招聘看板平台介绍.pptx', added: 0, removed: 0, status: 'added', binary: true },
    ]);
  });

  it('recovers a legacy terminal result from --output even when tool_call result was not persisted', () => {
    const output = '/Users/test/task_workspaces/default/最终结果.pptx';
    const item: BackendHistoryItem = {
      role: 'assistant',
      content: 'PPT 已生成',
      turn_file_changes: [
        { path: '/Users/test/task_workspaces/default/ppt/slide01.svg', name: 'slide01.svg', added: 12, removed: 0, status: 'added' },
      ],
      tool_calls: [
        {
          id: 'terminal-build',
          name: 'terminal',
          arguments: { command: `node build.js --output "${output}" 2>&1` },
          result: '',
          status: 'done',
        },
      ],
    };

    expect(mapBackendHistoryItem(item).turnFileChanges?.map((file) => file.path)).toEqual([
      '/Users/test/task_workspaces/default/ppt/slide01.svg',
      output,
    ]);
    expect(mapBackendHistoryItem(item).turnFileChanges?.[1]).toMatchObject({ binary: true, status: 'added' });
  });

  it('keeps a browser screenshot result when hydrating history', () => {
    const path = '/home/u/.Crew/accounts/acct_0123456789abcdef/task_workspaces/ws/downloads/browser/shot.png';
    const msg = mapBackendHistoryItem({
      role: 'assistant',
      content: '',
      tool_calls: [{
        id: 'shot-1',
        name: 'browser_use',
        arguments: { action: 'screenshot' },
        result: path,
        status: 'done',
      }],
    });

    expect(msg.toolCalls?.[0]?.result).toBe(path);
    expect(msg.toolCalls?.[0]?.status).toBe('done');
  });

  it('normalizes transient generating history without losing completed calls', () => {
    const active = mapBackendHistoryItem({
      role: 'assistant',
      content: '',
      tool_calls: [{ id: 'active', name: 'file_write', arguments: {}, status: 'generating' }],
    });
    const completed = mapBackendHistoryItem({
      role: 'assistant',
      content: '',
      tool_calls: [{ id: 'done', name: 'file_write', arguments: {}, status: 'generating', duration: 0.5 }],
    });

    expect(active.toolCalls?.[0]?.status).toBe('running');
    expect(completed.toolCalls?.[0]?.status).toBe('done');
  });
});

describe('Team Session history mapping', () => {
  it('preserves team_internal identity and presentation metadata', () => {
    const msg = mapBackendHistoryItem({
      role: 'team_internal',
      content: '@leader 已完成',
      agent_id: 'hermes',
      agent_name: 'Hermes',
      agent_role: '全栈开发',
      event_type: 'team_submit',
      node_id: 'implement_core',
      source_session_id: 'parent::turn::request::hermes',
      display_mode: 'chat',
      artifacts: [{ title: 'game2048.js', path: '/tmp/game2048.js' }],
    });

    expect(msg).toMatchObject({
      role: 'team_internal',
      content: '@leader 已完成',
      agentId: 'hermes',
      agentName: 'Hermes',
      agentRole: '全栈开发',
      eventType: 'team_submit',
      nodeId: 'implement_core',
      displayMode: 'chat',
    });
    expect(msg.artifacts?.[0]?.title).toBe('game2048.js');
  });

  it('merges streaming text, thinking and tool updates for the same node', () => {
    const first = mapBackendHistoryItem({
      role: 'team_internal',
      content: '正在',
      thinking: '分析',
      event_type: 'team_stream',
      display_mode: 'stream',
      node_id: 'implement_core',
      source_session_id: 'parent::turn::request::hermes',
      tool_calls: [{ id: 'tool-1', name: 'write', arguments: { path: 'game.js' }, status: 'running' }],
    });
    const second = mapBackendHistoryItem({
      role: 'team_internal',
      content: '实现',
      thinking: '代码',
      event_type: 'team_stream',
      display_mode: 'stream',
      node_id: 'implement_core',
      source_session_id: 'parent::turn::request::hermes',
      tool_calls: [{ id: 'tool-1', name: 'write', arguments: { path: 'game.js' }, status: 'done', result: 'ok' }],
    });
    const merged = mergeTeamInternalMessage([first], second, { append: true });

    expect(merged).toHaveLength(1);
    expect(merged[0].content).toBe('正在实现');
    expect(merged[0].thinking).toBe('分析代码');
    expect(merged[0].toolCalls?.[0]).toMatchObject({ toolCallId: 'tool-1', status: 'done', result: 'ok' });
  });

  it('uses the single-agent overlap merge for cumulative Team thinking frames', () => {
    const first = mapBackendHistoryItem({
      role: 'team_internal',
      content: '',
      thinking: '我先审阅成员提交。',
      event_type: 'team_stream',
      display_mode: 'stream',
      node_id: 'leader_review',
      source_session_id: 'parent::turn::request::leader',
    });
    const cumulative = mapBackendHistoryItem({
      role: 'team_internal',
      content: '',
      thinking: '我先审阅成员提交。接着检查验收条件。',
      event_type: 'team_stream',
      display_mode: 'stream',
      node_id: 'leader_review',
      source_session_id: 'parent::turn::request::leader',
    });

    const merged = mergeTeamInternalMessage([first], cumulative, { append: true });

    expect(merged[0].thinking).toBe('我先审阅成员提交。接着检查验收条件。');
  });

  it('replaces a node stream with its submitted result and retains the execution process', () => {
    const stream = mapBackendHistoryItem({
      role: 'team_internal',
      content: '实现过程',
      event_type: 'team_stream',
      node_id: 'implement_core',
      source_session_id: 'parent::turn::request::hermes',
      display_mode: 'stream',
      collapsed_title: '核心逻辑的执行过程',
    });
    const result = mapBackendHistoryItem({
      role: 'team_internal',
      content: '@leader 核心逻辑已完成',
      event_type: 'team_submit',
      node_id: 'implement_core',
      source_session_id: 'parent::turn::request::hermes',
      display_mode: 'chat',
    });
    const merged = mergeTeamInternalMessages([stream, result]);

    expect(merged).toHaveLength(1);
    expect(merged[0]).toMatchObject({
      eventType: 'team_submit',
      content: '@leader 核心逻辑已完成',
      processText: '实现过程',
      collapsedTitle: '核心逻辑的执行过程',
    });
  });

  it('deduplicates a live user mention answer already restored from history', () => {
    const history = mapBackendHistoryItem({
      role: 'team_internal',
      content: '我当前使用 K3 模型。',
      event_type: 'team_communication',
      source_session_id: 'desktop_demo::turn::mention_req_2::coder',
      agent_id: 'coder',
      communication_kind: 'user_mention_answer',
      communication_status: 'answered',
      request_id: 'mention_req_2',
      reply_to: 'bus_msg_2',
    });
    const live = { ...history, id: 'live-mention-answer' };

    const merged = mergeTeamInternalMessage([history], live);

    expect(merged).toHaveLength(1);
    expect(merged[0].requestId).toBe('mention_req_2');
    expect(merged[0].communicationStatus).toBe('answered');
  });

  it('replaces a waiting direct mention with the terminal answer by request id', () => {
    const waiting = mapBackendHistoryItem({
      role: 'team_internal',
      content: '正在询问 coder…',
      agent_id: 'coder',
      communication_kind: 'user_mention_answer',
      communication_status: 'waiting_reply',
      request_id: 'mention_req_waiting',
      communication_request_text: '你使用的是什么模型？',
    });
    const answered = {
      ...waiting,
      id: 'answered',
      content: '当前使用 K3 模型。',
      communicationStatus: 'answered',
      replyTo: 'bus_msg_waiting',
    };

    const merged = mergeTeamInternalMessage([waiting], answered);

    expect(merged).toHaveLength(1);
    expect(merged[0].content).toBe('当前使用 K3 模型。');
    expect(merged[0].communicationStatus).toBe('answered');
  });
});

describe('inferTurnFileChangesFromToolCalls', () => {
  it('dedupes by path keeping last write', () => {
    const files = inferTurnFileChangesFromToolCalls([
      { id: '1', name: 'file_write', arguments: { path: 'a.py' } },
      { id: '2', name: 'file_write', arguments: { path: 'a.py' } },
      { id: '3', name: 'terminal', arguments: { command: 'ls' } },
    ]);
    expect(files).toHaveLength(1);
    expect(files[0].path).toBe('a.py');
  });
});

describe('filterExistingTurnFileChanges', () => {
  it('keeps deleted status and drops missing added/modified paths', async () => {
    const pathExists = vi.fn(async (path: string) => !path.includes('gone'));
    (globalThis as { window?: unknown }).window = { Crew: { pathExists } };
    const files: TurnFileChangeSummary[] = [
      { path: 'gone.js', name: 'gone.js', added: 1, removed: 0, status: 'added' },
      { path: 'keep.html', name: 'keep.html', added: 2, removed: 0, status: 'added' },
      { path: 'removed.py', name: 'removed.py', added: 0, removed: 3, status: 'deleted' },
    ];
    const out = await filterExistingTurnFileChanges(files);
    expect(out.map((f) => f.path)).toEqual(['keep.html', 'removed.py']);
    expect(pathExists).toHaveBeenCalled();
  });
});
