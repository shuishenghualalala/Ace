/**
 * renderAgentTurn 单测：验证执行中/已完成状态的文案、spinner、open 状态。
 *
 * liveness 模型：isLive = options.isStreaming（per-turn 自有信号）。使用 per-turn 信号
 * 可避免新回合把已封口回合重新标记为执行中，因此用例以 isStreaming:true
 *（+ message streaming:true）驱动。
 * @vitest-environment happy-dom
 */
import { describe, it, expect, vi } from 'vitest';
import {
  renderAgentTurn,
  renderTeamInternalMessage,
  renderTypingIndicator,
  toolIconKind,
  type ChatMessage,
} from '../../src/ui/chat-render';

function makeMessages(overrides: Partial<ChatMessage> = {}): ChatMessage[] {
  return [
    {
      id: 'turn-1',
      role: 'assistant',
      content: '最终回答。',
      timestamp: 1_700_000_000_000,
      streaming: false,
      thinking: '思考过程内容',
      ...overrides,
    } as ChatMessage,
  ];
}

describe('toolIconKind（时间线工具图标分类）', () => {
  it('按工具语义分类', () => {
    expect(toolIconKind('file_write')).toBe('write');
    expect(toolIconKind('patch')).toBe('write');
    expect(toolIconKind('file_read')).toBe('read');
    expect(toolIconKind('grep')).toBe('search');
    expect(toolIconKind('glob')).toBe('search');
    expect(toolIconKind('web_search')).toBe('web');
    expect(toolIconKind('browser_open')).toBe('web');
    expect(toolIconKind('todo')).toBe('todo');
    expect(toolIconKind('memory')).toBe('memory');
    expect(toolIconKind('delegate_to_teammate')).toBe('team');
    expect(toolIconKind('run_agent')).toBe('team');
    expect(toolIconKind('skill_view')).toBe('skill');
    expect(toolIconKind('cron_create')).toBe('cron');
    expect(toolIconKind('terminal')).toBe('terminal');
    expect(toolIconKind('custom_mcp_tool')).toBe('terminal');
  });
});

describe('renderAgentTurn', () => {
  it('普通 Crew 回合与生成占位复用智能体页 Q 版耳机机器人头像', () => {
    const turn = renderAgentTurn(makeMessages({ thinking: undefined }), {
      isStreaming: false,
      userPinnedOpen: null,
      turnDurationMs: 1_000,
    });
    const typing = renderTypingIndicator();

    for (const root of [turn, typing]) {
      expect(root.classList.contains('msg--agent-turn')).toBe(true);
      const symbol = root.querySelector<SVGUseElement>('.msg__avatar-symbol use');
      expect(symbol?.getAttribute('href')).toBe('./crew-ui-symbols.svg#avatar-headphones');
      expect(root.querySelector('.msg__avatar-media')).toBeNull();
    }
  });

  it('嵌入式清爽对话可隐藏助手身份行', () => {
    const turn = renderAgentTurn(makeMessages({ thinking: undefined }), {
      isStreaming: false,
      userPinnedOpen: null,
      turnDurationMs: 1_000,
      showAssistantName: false,
    });

    expect(turn.querySelector('.msg__name')).toBeNull();
    expect(turn.textContent).toContain('最终回答。');
  });

  it('ACP 回合只在显式身份下展示外部智能体头像与名称', () => {
    const root = renderAgentTurn(makeMessages(), {
      isStreaming: false,
      userPinnedOpen: null,
      turnDurationMs: 1_000,
      identity: { kind: 'external', name: 'Kimi', badge: 'K' },
    });
    expect(root.querySelector('.msg__name')?.textContent).toBe('Kimi');
    expect(root.classList.contains('msg--agent-turn')).toBe(true);
    expect(root.querySelector('.msg__avatar--external')?.textContent).toBe('K');
    expect(root.querySelector('.msg__avatar-media')).toBeNull();
  });

  it('外部 Team 回合展示团队名称与双智能体图标', () => {
    const root = renderAgentTurn(makeMessages(), {
      isStreaming: false,
      userPinnedOpen: null,
      turnDurationMs: 1_000,
      identity: { kind: 'team', name: '研发团队', badge: 'T' },
    });
    expect(root.querySelector('.msg__name')?.textContent).toBe('研发团队');
    expect(root.querySelectorAll('.session__team-logo i')).toHaveLength(2);
    expect(root.querySelector('.msg__team-logo')).toBeNull();
  });

  it('外部 Team 等待态继续复用侧栏 Team Logo', () => {
    const root = renderTypingIndicator({ kind: 'team', name: '研发团队', badge: 'T' });
    expect(root.querySelector('.msg__name')?.textContent).toBe('研发团队');
    expect(root.querySelectorAll('.session__team-logo i')).toHaveLength(2);
  });

  it('ACP 等待首片响应时保持外部身份，不回退 Crew', () => {
    const root = renderTypingIndicator({ kind: 'external', name: 'Kimi', badge: 'K' });
    expect(root.querySelector('.msg__name')?.textContent).toBe('Kimi');
    expect(root.querySelector('.msg__avatar--external')?.textContent).toBe('K');
    expect(root.querySelector('.msg__avatar-media')).toBeNull();
  });

  it('Team 内置成员使用新版 Crew SVG 头像并显示 leader 身份', () => {
    const root = renderTeamInternalMessage({
      id: 'team-leader',
      role: 'team_internal',
      content: '已派发任务。',
      timestamp: 1_700_000_000_000,
      agentId: 'crew::builtin',
      agentName: 'Crew',
      agentRole: '项目经理',
      isLeader: true,
      eventType: 'team_assign',
    });
    expect(root.classList.contains('team-internal')).toBe(true);
    expect(root.querySelector('.team-internal__name strong')?.textContent).toBe('Crew');
    expect(root.querySelector('.team-internal__name em')?.textContent).toBe('leader');
    expect(root.querySelector<SVGUseElement>('.team-internal__avatar .msg__avatar-symbol use')?.getAttribute('href'))
      .toBe('./crew-ui-symbols.svg#avatar-headphones');
  });

  it('Team 外部成员展示成员气泡、执行过程和产物卡', () => {
    const root = renderTeamInternalMessage({
      id: 'team-hermes',
      role: 'team_internal',
      content: '@leader 核心逻辑已完成',
      timestamp: 1_700_000_000_000,
      agentId: 'hermes',
      agentName: 'Hermes',
      agentRole: '全栈开发',
      agentTone: 2,
      eventType: 'team_submit',
      processText: '分析并编写核心算法',
      collapsedTitle: '核心逻辑的执行过程',
      artifacts: [{ title: 'game2048.js', path: '/tmp/game2048.js', kind: 'text' }],
      turnFileChanges: [
        { path: '/tmp/game2048.js', name: 'game2048.js', added: 24, removed: 3, status: 'modified' },
        { path: '/tmp/old.js', name: 'old.js', added: 0, removed: 5, status: 'deleted' },
      ],
    });
    expect(root.querySelector('.agent-avatar--message.agent-tone-2')?.textContent).toBe('H');
    expect(root.querySelector('.team-internal__bubble--tone-2')).not.toBeNull();
    expect(root.querySelector('.msg__foldable')).not.toBeNull();
    expect(root.querySelector('.msg__fold-label')?.textContent).toContain('已思考');
    expect(root.querySelector('.team-internal__collapse')).toBeNull();
    expect(root.querySelector('.team-internal__bubble')?.textContent).toContain('核心逻辑已完成');
    expect(root.querySelector('.team-artifact__body strong')?.textContent).toBe('game2048.js');
    expect(root.querySelector('.msg__file-changes')?.textContent).toContain('已编辑 2 个文件');
    expect(root.querySelector('[data-file-status="deleted"] .msg__file-changes__reveal'))
      .toHaveProperty('disabled', true);
  });

  it('Team 外部成员标题压缩完整 Markdown 职责提示', () => {
    const root = renderTeamInternalMessage({
      id: 'team-mention-role',
      role: 'team_internal',
      content: '我现在使用 Kimi Code。',
      timestamp: 1_700_000_000_000,
      agentId: 'kk',
      agentName: 'kk',
      agentRole: '### 全栈开发 - kk ##### 工作原则 - 先确认目标、输入、输出和验收标准，再执行。 - 团队协作关系 - 向 Leader 汇报。',
      communicationKind: 'user_mention_answer',
      communicationStatus: 'answered',
    });

    expect(root.querySelector('.team-internal__name em')?.textContent).toBe('全栈开发 - kk');
    expect(root.querySelector('.team-internal__name')?.textContent).not.toContain('工作原则');
    expect(root.querySelector('.team-internal__communication-status')?.textContent).toBe('已回答');
  });

  it('Team 成员通信标题优先展示结构化收件人', () => {
    const root = renderTeamInternalMessage({
      id: 'team-submit-to-leader',
      role: 'team_internal',
      content: '@leader 汇报当前执行状态',
      timestamp: 1_700_000_000_000,
      agentId: 'kk',
      agentName: 'kk',
      agentRole: '向 kk 征询执行意见与状态',
      mentionFrom: 'kk',
      mentionTo: ['leader'],
      mentionIntent: 'submit',
      eventType: 'team_submit',
    });
    expect(root.querySelector('.team-internal__name em')?.textContent)
      .toBe('向 leader 征询执行意见与状态');
  });

  it('直接 mention 失败可重试，运行中可取消', () => {
    const failed = renderTeamInternalMessage({
      id: 'mention-failed',
      role: 'team_internal',
      content: '回答失败',
      timestamp: 1_700_000_000_000,
      agentId: 'coder',
      agentName: 'coder',
      communicationKind: 'user_mention_answer',
      communicationStatus: 'failed',
      requestId: 'mention-1',
      communicationRequestText: '你使用的是什么模型？',
    });
    expect(failed.querySelector('[data-team-communication-action="retry"]')?.textContent).toBe('重试');

    const waiting = renderTeamInternalMessage({
      id: 'mention-waiting',
      role: 'team_internal',
      content: '正在询问 coder…',
      timestamp: 1_700_000_000_000,
      agentId: 'coder',
      agentName: 'coder',
      communicationKind: 'user_mention_answer',
      communicationStatus: 'waiting_reply',
      requestId: 'mention-2',
      communicationRequestText: '你使用的是什么模型？',
    });
    expect(waiting.querySelector('[data-team-communication-action="cancel"]')?.textContent).toBe('取消');
  });

  it('Team 规划运行中复用 Agent Turn 实时计时，以团队名称和 Team Logo 展示', () => {
    vi.useFakeTimers();
    vi.setSystemTime(1_700_000_003_400);
    const root = renderTeamInternalMessage({
      id: 'team-planning-live',
      role: 'team_internal',
      content: '',
      timestamp: 1_700_000_003_400,
      turnStartedAt: 1_700_000_001_000,
      agentId: 'crew::builtin',
      agentName: '像素开发小游戏团队',
      agentRole: '项目经理',
      isLeader: true,
      eventType: 'team_planning_progress',
      processText: '正在生成任务依赖关系',
      collapsedTitle: 'Crew 正在规划团队协作',
      streaming: true,
    }, true);

    expect(root.querySelectorAll('.session__team-logo i')).toHaveLength(2);
    expect(root.querySelector('.msg__avatar-media')).toBeNull();
    expect(root.querySelector('.team-internal__name strong')?.textContent).toBe('像素开发小游戏团队');
    expect(root.querySelector('.team-internal__name em')).toBeNull();
    expect(root.querySelector<HTMLDetailsElement>('details.msg__foldable')?.open).toBe(true);
    expect(root.querySelector('.msg__fold-label')?.textContent)
      .toBe('Crew 正在规划团队协作 · 已等待 2s');
    vi.useRealTimers();
  });

  it('Team 规划完成后自动折叠并保留最终耗时', () => {
    const root = renderTeamInternalMessage({
      id: 'team-planning-done',
      role: 'team_internal',
      content: '',
      timestamp: 1_700_000_004_200,
      turnStartedAt: 1_700_000_001_000,
      turnDurationMs: 3_200,
      agentId: 'crew::builtin',
      agentName: '像素开发小游戏团队',
      agentRole: '项目经理',
      isLeader: true,
      eventType: 'team_planning_progress',
      processText: '已生成 3 个节点',
      collapsedTitle: 'Crew 已生成团队执行图',
      streaming: false,
    }, false);

    expect(root.querySelectorAll('.session__team-logo i')).toHaveLength(2);
    expect(root.querySelector<HTMLDetailsElement>('details.msg__foldable')?.open).toBe(false);
    expect(root.querySelector('.msg__fold-label')?.textContent)
      .toBe('Crew 已生成团队执行图 · 3s');
  });

  it('已完成回合保留「已思考」入口且有正文时默认折叠过程区', () => {
    const root = renderAgentTurn(makeMessages(), {
      isStreaming: false,
      userPinnedOpen: null,
      turnDurationMs: 5_000,
    });
    const html = root.outerHTML;
    const details = root.querySelector('details.msg__foldable');
    expect(html).toContain('已思考 5s');
    expect(root.querySelector('.msg__fold-label')?.textContent).toBe('已思考 5s');
    expect(html).not.toContain('正在执行');
    expect(html).not.toContain('msg__fold-spinner');
    expect(details).not.toBeNull();
    // 正文已确认 → 过程区保持折叠（与流式正文到来时自动折一致）
    expect(details?.open).toBe(false);
    expect(details?.classList.contains('msg__foldable--live')).toBe(false);
  });

  it('thinking 渲染为时间线项，完成后默认收起', () => {
    const root = renderAgentTurn(makeMessages(), {
      isStreaming: false,
      userPinnedOpen: true,
      turnDurationMs: 5_000,
    });

    // 时间线结构：回合折叠 details + 思考项 details
    const item = root.querySelector('.process-timeline__item[data-thinking-for]');
    expect(item).not.toBeNull();
    const details = item?.querySelector('details.process-timeline__details');
    expect(details?.open).toBe(false);
    expect(item?.querySelector('.process-timeline__title')?.textContent).toBe('思考已完成');
    expect(item?.querySelector('.process-timeline__thinking')?.textContent).toBe('思考过程内容');
  });

  it('流式思考中时间线项默认展开且标题为「思考中」', () => {
    const root = renderAgentTurn(makeMessages({ streaming: true, content: '' }), {
      isStreaming: true,
      userPinnedOpen: null,
      turnDurationMs: 3_000,
    });
    const item = root.querySelector('.process-timeline__item[data-thinking-for]');
    expect(item?.querySelector('details.process-timeline__details')?.open).toBe(true);
    expect(item?.querySelector('.process-timeline__title')?.textContent).toBe('思考中');
    expect(item?.querySelector('.process-timeline__icon--running')).not.toBeNull();
  });

  it('已完成用户手动展开时保持展开', () => {
    const root = renderAgentTurn(makeMessages(), {
      isStreaming: false,
      userPinnedOpen: true,
      turnDurationMs: 5_000,
    });
    expect(root.querySelector('details.msg__foldable')?.open).toBe(true);
  });

  it('执行中（isStreaming=true）有过程内容时显示「正在执行」+ 已等待时间 + spinner 且默认展开', () => {
    const root = renderAgentTurn(makeMessages({ streaming: true, content: '' }), {
      isStreaming: true,
      userPinnedOpen: null,
      turnDurationMs: 3_000,
    });
    const html = root.outerHTML;
    const details = root.querySelector('details.msg__foldable');
    expect(html).toContain('正在执行 · 已等待 3s');
    expect(html).not.toContain('正在思考');
    expect(html).not.toContain('已处理');
    expect(html).toContain('msg__fold-spinner');
    expect(details?.classList.contains('msg__foldable--live')).toBe(true);
    expect(root.querySelector('.msg__fold-summary--live')).not.toBeNull();
    expect(details?.open).toBe(true);
  });

  it('发送后尚无过程内容时显示「正在思考」折叠条（乐观占位）', () => {
    const root = renderAgentTurn(
      [{
        id: 'turn-optimistic',
        role: 'assistant',
        content: '',
        timestamp: 1_700_000_000_000,
        streaming: true,
        turnStartedAt: 1_700_000_000_000,
      }],
      {
        isStreaming: true,
        userPinnedOpen: null,
        turnDurationMs: 12_000,
      },
    );
    const html = root.outerHTML;
    const details = root.querySelector('details.msg__foldable');
    expect(html).toContain('正在思考 · 已等待 12s');
    expect(html).not.toContain('正在执行');
    expect(html).toContain('msg__fold-spinner');
    expect(details).not.toBeNull();
    expect(details?.open).toBe(true);
    // 折叠条本身已是活着感，不再叠一层 typing 三点
    expect(root.querySelector('.typing-inline')).toBeNull();
  });

  it('执行中底部时间使用当前时间而不是首 token 时间', () => {
    const liveNow = new Date(2024, 0, 15, 16, 6, 0).getTime();
    const oldTime = new Date(2024, 0, 15, 16, 0, 0).getTime();
    vi.spyOn(Date, 'now').mockReturnValue(liveNow);
    const root = renderAgentTurn(makeMessages({ streaming: true, timestamp: oldTime }), {
      isStreaming: true,
      userPinnedOpen: null,
      turnDurationMs: 3_000,
    });
    expect(root.textContent).toContain('16:06');
    expect(root.textContent).not.toContain('16:00');
    vi.restoreAllMocks();
  });

  it('乐观占位在首个 thinking 到达后文案切到「正在执行」', () => {
    const root = renderAgentTurn(
      [{
        id: 'turn-1',
        role: 'assistant',
        content: '',
        timestamp: 1_700_000_000_000,
        streaming: true,
        thinking: '先读一下现有文件',
        turnStartedAt: 1_700_000_000_000,
      }],
      {
        isStreaming: true,
        userPinnedOpen: null,
        turnDurationMs: 5_000,
      },
    );
    expect(root.outerHTML).toContain('正在执行 · 已等待 5s');
    expect(root.outerHTML).not.toContain('正在思考');
  });

  it('正文首字出现后流式中即默认折叠过程区', () => {
    const root = renderAgentTurn(
      makeMessages({ streaming: true, content: '这', segmentRole: 'answer' }),
      {
        isStreaming: true,
        userPinnedOpen: null,
        turnDurationMs: 3_000,
      },
    );
    const details = root.querySelector('details.msg__foldable');
    // 流式中正文已开始 → 折叠过程区聚焦正文（外开内折的"开"只对无正文/已完成回合成立）。
    expect(details?.open).toBe(false);
    expect(details?.classList.contains('msg__foldable--live')).toBe(true);
    expect(root.querySelector('.msg__body > .msg__text')?.textContent).toContain('这');
  });

  it('执行中尊重用户手动折叠', () => {
    const root = renderAgentTurn(makeMessages({ streaming: true, content: '' }), {
      isStreaming: true,
      userPinnedOpen: false,
      turnDurationMs: 3_000,
    });
    const details = root.querySelector('details.msg__foldable');
    expect(details?.open).toBe(false);
    // 但 live 标记样式仍在（说明这是执行中回合，只是用户选择折叠了）
    expect(details?.classList.contains('msg__foldable--live')).toBe(true);
  });

  it('执行中用户手动展开时保持展开', () => {
    const root = renderAgentTurn(makeMessages({ streaming: true }), {
      isStreaming: true,
      userPinnedOpen: true,
      turnDurationMs: 3_000,
    });
    expect(root.querySelector('details.msg__foldable')?.open).toBe(true);
  });

  it('工具时间线项默认折叠，展示语义化标题与 Request 块', () => {
    const root = renderAgentTurn(
      makeMessages({
        content: '最终回答。',
        thinking: undefined,
        toolCalls: [
          {
            toolCallId: 't1',
            name: 'terminal',
            args: '{"command":"ls -lah ~/Desktop/"}',
            status: 'done',
            startedAt: 1_700_000_000_000,
            duration: 47,
          },
        ],
      }),
      {
        isStreaming: false,
        userPinnedOpen: null,
        turnDurationMs: 5_000,
      },
    );
    const details = root.querySelector('details.process-timeline__details');
    expect(details?.open).toBe(false);
    expect(details?.getAttribute('data-fold-key')).toBe('tool:turn-1:t1');
    expect(details?.querySelector('.process-timeline__title')?.textContent)
      .toBe('运行 ls -lah ~/Desktop/');
    // args 以 pretty JSON 呈现在 Request 代码块
    expect(details?.querySelector('[data-section="args"] pre')?.textContent)
      .toBe('{\n  "command": "ls -lah ~/Desktop/"\n}');
    // 无 result → Response 块被移除
    expect(details?.querySelector('[data-section="result"]')).toBeNull();
  });

  it('无参数无结果的工具渲染为静态时间线行（不可展开）', () => {
    const root = renderAgentTurn(
      makeMessages({
        content: '最终回答。',
        thinking: undefined,
        toolCalls: [
          {
            toolCallId: 't1',
            name: 'skill_view',
            status: 'done',
            startedAt: 1_700_000_000_000,
            duration: 12,
          },
        ],
      }),
      {
        isStreaming: false,
        userPinnedOpen: null,
        turnDurationMs: 5_000,
      },
    );
    expect(root.querySelector('details.process-timeline__details')).toBeNull();
    const staticRow = root.querySelector('.process-timeline__row--static');
    expect(staticRow).not.toBeNull();
    expect(staticRow?.querySelector('.process-timeline__title')?.textContent).toBe('查看技能');
  });

  it('run_agent 渲染 subagent 专用卡片：中文标题 + 任务描述 + 执行摘要', () => {
    const root = renderAgentTurn(
      makeMessages({
        content: '最终回答。',
        thinking: undefined,
        toolCalls: [
          {
            toolCallId: 'sa1',
            name: 'run_agent',
            args: JSON.stringify({
              agent_type: 'code-explorer',
              goal: '测试 subagent 基本功能，列出 Crew 项目的第一级子目录结构',
              context: '只列出深度第一层，不要递归',
            }),
            result: JSON.stringify({
              results: [{
                agent: 'code-explorer',
                status: 'completed',
                summary: '共 32 个条目',
                duration_seconds: 5.38,
                tool_calls: 1,
              }],
            }),
            status: 'done',
            startedAt: 1_700_000_000_000,
            duration: 5380,
          },
        ],
      }),
      { isStreaming: false, userPinnedOpen: null, turnDurationMs: 8_000 },
    );
    const details = root.querySelector<HTMLDetailsElement>('details.process-timeline__details.subagent-card');
    expect(details).not.toBeNull();
    // 与其他工具卡一致：默认折叠 + fold-key 持久化
    expect(details?.open).toBe(false);
    expect(details?.getAttribute('data-fold-key')).toBe('tool:turn-1:sa1');
    expect(details?.querySelector('.subagent-card__title')?.textContent)
      .toBe('子智能体 code-explorer：测试 subagent 基本功能，列出 Crew 项目的第一级子目录结构');
    const goal = details?.querySelector('.subagent-card__goal');
    expect(goal?.textContent).toBe('测试 subagent 基本功能，列出 Crew 项目的第一级子目录结构');
    expect(details?.querySelector('.subagent-card__context')?.textContent).toBe('只列出深度第一层，不要递归');
    // 子智能体最终回复入卡（过长时由 CSS max-height + 滚动条收敛）
    expect(details?.querySelector('.subagent-card__reply-label')?.textContent).toBe('最终回复');
    expect(details?.querySelector('.subagent-card__reply-text')?.textContent).toBe('共 32 个条目');
    expect(details?.querySelector('.subagent-card__meta')?.textContent)
      .toBe('执行摘要：1 次工具调用，耗时 5s');
    // 专属卡片不走普通工具的 Request/Response 代码块
    expect(details?.querySelector('.process-code-block')).toBeNull();
  });

  it('delegate_task 批量模式：一张卡内编号列出每个子任务及各自摘要', () => {
    const root = renderAgentTurn(
      makeMessages({
        content: '最终回答。',
        thinking: undefined,
        toolCalls: [
          {
            toolCallId: 'sa2',
            name: 'delegate_task',
            args: JSON.stringify({
              tasks: [
                { goal: '调研 A 方案' },
                { goal: '调研 B 方案' },
              ],
            }),
            result: JSON.stringify({
              results: [
                { agent: 'task#0', status: 'completed', summary: '', duration_seconds: 12, tool_calls: 3 },
                { agent: 'task#1', status: 'timeout', summary: '无活动超时', duration_seconds: 120, tool_calls: 7 },
              ],
            }),
            status: 'done',
            startedAt: 1_700_000_000_000,
            duration: 120_000,
          },
        ],
      }),
      { isStreaming: false, userPinnedOpen: null, turnDurationMs: 125_000 },
    );
    const details = root.querySelector<HTMLDetailsElement>('details.subagent-card');
    expect(details?.querySelector('.subagent-card__title')?.textContent).toBe('子智能体：2 个并行任务');
    const goals = [...(details?.querySelectorAll('.subagent-card__goal') ?? [])].map((el) => el.textContent);
    expect(goals).toEqual(['1. 调研 A 方案', '2. 调研 B 方案']);
    const metas = [...(details?.querySelectorAll('.subagent-card__meta') ?? [])].map((el) => el.textContent);
    expect(metas).toEqual(['执行摘要：3 次工具调用，耗时 12s', '已超时中止：7 次工具调用，耗时 2m']);
    expect(details?.querySelector('.subagent-card__meta--error')).not.toBeNull();
  });

  it('run_agent 运行中：摘要行显示执行中，耗时走实时轮询', () => {
    const root = renderAgentTurn(
      makeMessages({
        content: '',
        thinking: undefined,
        streaming: true,
        toolCalls: [
          {
            toolCallId: 'sa3',
            name: 'run_agent',
            args: JSON.stringify({ agent_type: 'code-explorer', goal: '分析目录结构' }),
            status: 'running',
            startedAt: 1_700_000_000_000,
          },
        ],
      }),
      { isStreaming: true, userPinnedOpen: null, turnDurationMs: 3_000 },
    );
    const details = root.querySelector<HTMLDetailsElement>('details.subagent-card');
    expect(details?.querySelector('.subagent-card__title')?.textContent).toBe('子智能体 code-explorer：分析目录结构');
    expect(details?.querySelector('.subagent-card__meta')?.textContent).toBe('执行中…');
    const dur = details?.querySelector('.process-timeline__duration');
    expect(dur?.getAttribute('data-active')).toBe('true');
    // running 图标在时间线项的图标列（details 之外）
    expect(root.querySelector('.process-timeline__icon--running')).not.toBeNull();
  });

  it('run_agent 后台启动：卡片提示已转后台与任务 ID', () => {
    const root = renderAgentTurn(
      makeMessages({
        content: '最终回答。',
        thinking: undefined,
        toolCalls: [
          {
            toolCallId: 'sa4',
            name: 'run_agent',
            args: JSON.stringify({ agent_type: 'code-explorer', goal: '长时间跑批', run_in_background: true }),
            result: JSON.stringify({ status: 'launched', task_id: 'bg-123', agent: 'code-explorer' }),
            status: 'done',
            startedAt: 1_700_000_000_000,
            duration: 300,
          },
        ],
      }),
      { isStreaming: false, userPinnedOpen: null, turnDurationMs: 5_000 },
    );
    const details = root.querySelector<HTMLDetailsElement>('details.subagent-card');
    expect(details?.querySelector('.subagent-card__meta')?.textContent)
      .toBe('已转后台运行（任务 ID：bg-123），完成后将推送通知');
  });

  it('浏览器截图提供查看、复制与定位操作，而不是不可交互的裸图片', () => {
    const shotPath = '/home/u/.Crew/accounts/acct_0123456789abcdef/task_workspaces/ws/downloads/browser/shot.png';
    const root = renderAgentTurn(
      makeMessages({
        toolCalls: [{
          toolCallId: 'shot-1',
          name: 'browser_use',
          args: '{"action":"screenshot"}',
          result: shotPath,
          status: 'done',
          startedAt: 1_700_000_000_000,
        }],
      }),
      { isStreaming: false, userPinnedOpen: null, turnDurationMs: 5_000 },
    );
    const view = root.querySelector<HTMLButtonElement>('[data-image-view-src]');
    expect(view?.getAttribute('data-image-local-path')).toBe(shotPath);
    expect(view?.getAttribute('aria-label')).toContain('查看大图');
    expect(root.querySelector('[data-image-copy-path]')?.getAttribute('data-image-copy-path')).toBe(shotPath);
    expect(root.querySelector('[data-image-reveal-path]')?.getAttribute('data-image-reveal-path')).toBe(shotPath);
    expect(root.querySelector('[data-image-copy-path]')?.getAttribute('data-tooltip')).toBe('复制图片');
    expect(root.querySelector('[data-image-copy-path]')?.textContent).toBe('');
    expect(root.querySelector('[data-image-copy-path] svg')).not.toBeNull();
    expect(root.querySelector<HTMLImageElement>('.tool-card__image')?.draggable).toBe(false);
  });

  it('带工具调用的推理旁白在执行中也进折叠区，且渲染在工具卡之上', () => {
    // 模型先说"我来帮您查询…"再发 tool_call；二者落在同一条 assistant message 上
    // （toolReducer 把 toolCalls patch 到承载 content 的当前 assistantId）。
    // 渲染顺序必须跟随产出时序：旁白在前、工具卡在后。改前这里反了（工具在上）。
    const root = renderAgentTurn(
      makeMessages({
        content: '我来帮您查询今天的黄金价格。',
        thinking: undefined,
        streaming: true,
        toolCalls: [
          {
            toolCallId: 't1',
            name: 'skill_view',
            status: 'running',
            startedAt: 1_700_000_000_000,
          },
        ],
      }),
      {
        isStreaming: true,
        userPinnedOpen: null,
        turnDurationMs: 9_000,
      },
    );
    const foldContent = root.querySelector('.msg__fold-content');
    expect(foldContent?.textContent).toContain('我来帮您查询今天的黄金价格');
    expect(root.querySelector('.msg__body > .msg__foldable + .msg__text')).toBeNull();
    const timeline = foldContent?.querySelector('.process-timeline');
    expect(timeline).not.toBeNull();
    const items = Array.from(timeline!.children);
    expect(items).toHaveLength(2);
    // 时间线顺序跟随产出时序：旁白项在前、工具项在后。
    expect(items[0]?.querySelector('.process-timeline__narration')).not.toBeNull();
    expect(items[1]?.querySelector('.process-timeline__row--static')).not.toBeNull();
  });

  it('最终答案流式输出时保持在折叠区外', () => {
    const root = renderAgentTurn(
      makeMessages({
        content: '这是给用户的正式回答',
        streaming: true,
        thinking: undefined,
      }),
      {
        isStreaming: true,
        userPinnedOpen: null,
        turnDurationMs: 3_000,
      },
    );
    expect(root.querySelector('.msg__body > .msg__text')?.textContent).toContain('这是给用户的正式回答');
    expect(root.querySelector('.msg__fold-content')?.textContent ?? '').not.toContain('这是给用户的正式回答');
  });

  it('回合结束后最后一条正文移到折叠区外', () => {
    const root = renderAgentTurn(
      makeMessages({ content: '抱歉，由于网络限制，我无法实时获取今日黄金价格数据。', thinking: undefined }),
      {
        isStreaming: false,
        userPinnedOpen: null,
        turnDurationMs: 51_000,
      },
    );
    expect(root.querySelector('.msg__fold-content .msg__text:not(.process-timeline__narration)')).toBeNull();
    expect(root.querySelector('.msg__body > .msg__text')?.textContent).toContain('抱歉，由于网络限制');
  });

  it('中间旁白在已完成时进折叠区', () => {
    const messages: ChatMessage[] = [
      { id: 'm1', role: 'assistant', content: '我先查一下。', timestamp: 1_700_000_000_000 },
      { id: 'm2', role: 'assistant', content: '最终回答。', timestamp: 1_700_000_001_000 },
    ];
    const root = renderAgentTurn(messages, {
      isStreaming: false,
      userPinnedOpen: null,
      turnDurationMs: 5_000,
    });
    expect(root.outerHTML).toContain('process-timeline__narration');
  });

  it('正文曾出现后仅余过程内容时保持折叠（防流式回弹）', () => {
    const messages: ChatMessage[] = [
      {
        id: 'm1',
        role: 'assistant',
        content: '我先查一下。',
        timestamp: 1_700_000_000_000,
        segmentRole: 'process',
      },
      {
        id: 'm2',
        role: 'assistant',
        content: '',
        timestamp: 1_700_000_001_000,
        streaming: true,
        segmentRole: 'process',
        toolCalls: [
          { toolCallId: 't1', name: 'terminal', status: 'running', startedAt: 1_700_000_001_000 },
        ],
      },
    ];
    const root = renderAgentTurn(messages, {
      isStreaming: true,
      userPinnedOpen: false,
      turnDurationMs: 3_000,
    });
    expect(root.querySelector('details.msg__foldable')?.open).toBe(false);
  });

  it('中间旁白在执行中也进折叠区，保持和完成态结构一致', () => {
    const messages: ChatMessage[] = [
      { id: 'm1', role: 'assistant', content: '我先查一下。', timestamp: 1_700_000_000_000, segmentRole: 'process' },
      { id: 'm2', role: 'assistant', content: '最终回答。', timestamp: 1_700_000_001_000, streaming: true, segmentRole: 'answer' },
    ];
    const root = renderAgentTurn(messages, {
      isStreaming: true,
      userPinnedOpen: null,
      turnDurationMs: 3_000,
    });
    const html = root.outerHTML;
    expect(html).toContain('process-timeline__narration');
    expect(html).toContain('我先查一下。');
    expect(html).toContain('最终回答。');
    // 硬确认 answer 段已有正文 → 折叠过程区。
    expect(root.querySelector('details.msg__foldable')?.open).toBe(false);
  });

  it('被停止的半截回复不显示执行中，但仍按流式 markdown 容错', () => {
    const root = renderAgentTurn(
      makeMessages({ content: '- 📝 **', thinking: undefined, interrupted: true }),
      {
        isStreaming: false,
        userPinnedOpen: null,
        turnDurationMs: 5_000,
      },
    );
    const html = root.outerHTML;
    expect(html).not.toContain('正在执行');
    expect(html).not.toContain('****');
  });

  it('final 后（isStreaming=false）不走流式 markdown 入口，避免 ** 被错误补闭合', () => {
    const root = renderAgentTurn(
      makeMessages({ content: '**未闭合', streaming: false }),
      {
        isStreaming: false,
        userPinnedOpen: null,
        turnDurationMs: 3_000,
      },
    );
    const html = root.outerHTML;
    // 非流式入口不应补闭合，未闭合的 ** 应作为普通文本保留。
    expect(html).toContain('**未闭合');
    expect(html).not.toContain('<strong>未闭合</strong>');
  });

  it('先工具后文字的答案段显示在折叠区外', () => {
    const messages: ChatMessage[] = [
      {
        id: 'm1',
        role: 'assistant',
        content: '',
        timestamp: 1_700_000_000_000,
        segmentRole: 'process',
        toolCalls: [
          { toolCallId: 't1', name: 'web_search', status: 'done', startedAt: 1_700_000_000_000, duration: 100 },
        ],
      },
      {
        id: 'm2',
        role: 'assistant',
        content: '今日黄金价格为每克 520 元。',
        timestamp: 1_700_000_001_000,
        segmentRole: 'answer',
      },
    ];
    const root = renderAgentTurn(messages, {
      isStreaming: false,
      userPinnedOpen: null,
      turnDurationMs: 8_000,
    });
    expect(root.querySelector('.msg__body > .msg__text')?.textContent).toContain('今日黄金价格为');
    expect(root.querySelector('.msg__fold-content .msg__text:not(.process-timeline__narration)')).toBeNull();
  });

  it('流式旁白（工具尚未到达）时折叠条不消失且过程区保持展开', () => {
    // 计划模式常见：先吐「我来帮你规划…」，稍后才有 exit_plan_mode / file_write。
    // 不确定阶段标 process：进思考区、不触发自动折，避免「折上又展开」跳变。
    const root = renderAgentTurn(
      [{
        id: 'turn-1',
        role: 'assistant',
        content: '我来帮你规划一个俄罗斯方块游戏的开发计划。',
        timestamp: 1_700_000_000_000,
        streaming: true,
        segmentRole: 'process',
        turnStartedAt: 1_700_000_000_000,
      }],
      {
        isStreaming: true,
        userPinnedOpen: null,
        turnDurationMs: 8_000,
      },
    );
    const details = root.querySelector('details.msg__foldable');
    expect(details).not.toBeNull();
    expect(details?.open).toBe(true);
    expect(details?.classList.contains('msg__foldable--live')).toBe(true);
    expect(root.querySelector('.msg__fold-label')?.textContent).toContain('正在执行');
    expect(root.querySelector('.msg__fold-content')?.textContent).toContain('我来帮你规划');
    expect(root.querySelector('.msg__body > .msg__text')).toBeNull();
  });

  it('无过程内容时不渲染折叠区', () => {
    const root = renderAgentTurn(
      makeMessages({ content: '只有正文。', thinking: undefined }),
      {
        isStreaming: false,
        userPinnedOpen: null,
        turnDurationMs: 5_000,
      },
    );
    const html = root.outerHTML;
    expect(html).not.toContain('msg__foldable');
    expect(html).toContain('只有正文。');
  });

  it('带 activity 的瞬时状态：live 回合只显示最新一条', () => {
    const messages: ChatMessage[] = [
      {
        id: 'a1', role: 'assistant', content: '', timestamp: 1, streaming: true, segmentRole: 'process',
        toolCalls: [{ toolCallId: 't1', name: 'skills_list', status: 'done', startedAt: 1, duration: 300 }],
      },
      { id: 's1', role: 'status', content: '处理中…', timestamp: 2, activity: 'progress' },
      { id: 's2', role: 'status', content: '即将完成…', timestamp: 3, activity: 'progress' },
    ] as ChatMessage[];
    const root = renderAgentTurn(messages, {
      isStreaming: true,
      userPinnedOpen: null,
      turnDurationMs: 3_000,
    });
    const activities = Array.from(root.querySelectorAll('.process-timeline__item'))
      .filter((el) => el.textContent?.includes('中…') || el.textContent?.includes('完成…'));
    expect(activities).toHaveLength(1);
    expect(activities[0].textContent).toContain('即将完成…');
  });

  it('回合完成后瞬时活动状态全部隐藏，不影响工具项与折叠条', () => {
    const messages: ChatMessage[] = [
      {
        id: 'a1', role: 'assistant', content: '让我查看一下技能清单。', timestamp: 1, segmentRole: 'process',
        toolCalls: [{ toolCallId: 't1', name: 'skills_list', status: 'done', startedAt: 1, duration: 300 }],
      },
      { id: 's1', role: 'status', content: '处理中…', timestamp: 2, activity: 'progress' },
      { id: 's2', role: 'status', content: '即将完成…', timestamp: 3, activity: 'progress' },
      { id: 'a2', role: 'assistant', content: '我有这些技能。', timestamp: 4, segmentRole: 'answer' },
    ] as ChatMessage[];
    const root = renderAgentTurn(messages, {
      isStreaming: false,
      userPinnedOpen: null,
      turnDurationMs: 17_000,
    });
    const activities = Array.from(root.querySelectorAll('.process-timeline__item'))
      .filter((el) => el.textContent?.includes('中…') || el.textContent?.includes('完成…'));
    expect(activities).toHaveLength(0);
    // 工具项保留、折叠条正常计数
    expect(root.textContent).toContain('技能列表');
    expect(root.querySelector('.msg__fold-label')?.textContent).toBe('已执行 17s，已调用 1 个工具');
  });

  it('工具之间仅含空白的旁白不渲染占位项（时间线保持紧凑）', () => {
    // 模型在工具 loop 之间常吐分隔换行（'\n'/'  '），渲染成看不见的空白项会把两个
    // 工具项撑出大间距。空白 content 必须视为无内容。
    const messages: ChatMessage[] = [
      { id: 'm1', role: 'assistant', content: '我来帮你写一首诗并保存到桌面。', timestamp: 1, segmentRole: 'process' },
      {
        id: 'm2', role: 'assistant', content: '', timestamp: 2, segmentRole: 'process',
        toolCalls: [{ toolCallId: 't1', name: 'terminal', status: 'done', startedAt: 1, duration: 100, args: '{"command":"echo $HOME"}' }],
      },
      { id: 'm3', role: 'assistant', content: '\n', timestamp: 3, segmentRole: 'process' },
      {
        id: 'm4', role: 'assistant', content: '', timestamp: 4, segmentRole: 'process',
        toolCalls: [{ toolCallId: 't2', name: 'file_write', status: 'done', startedAt: 2, duration: 50, args: '{"path":"~/Desktop/x.txt"}' }],
      },
      { id: 'm5', role: 'assistant', content: '写好了。', timestamp: 5, segmentRole: 'answer' },
    ] as ChatMessage[];
    const root = renderAgentTurn(messages, {
      isStreaming: false,
      userPinnedOpen: null,
      turnDurationMs: 16_000,
    });
    const items = root.querySelectorAll('.process-timeline__item');
    // 旁白 + 2 个工具 = 3 项；空白 '\n' 不产生第 4 项
    expect(items).toHaveLength(3);
    expect(items[0]?.querySelector('.process-timeline__narration')).not.toBeNull();
    expect(items[1]?.textContent).toContain('运行 echo $HOME');
    expect(items[2]?.textContent).toContain('写入 x.txt');
    // 正文仍是正式回答，空白段不被当成答案
    expect(root.querySelector('.msg__body > .msg__text')?.textContent).toContain('写好了。');
  });

  it('完成回合有工具调用时折叠条显示「已执行 Xs，已调用 N 个工具」', () => {
    const root = renderAgentTurn(
      makeMessages({
        content: '最终回答。',
        thinking: undefined,
        toolCalls: [
          { toolCallId: 't1', name: 'terminal', status: 'done', startedAt: 1_700_000_000_000, duration: 100 },
          { toolCallId: 't2', name: 'file_read', status: 'done', startedAt: 1_700_000_000_100, duration: 50 },
        ],
      }),
      {
        isStreaming: false,
        userPinnedOpen: null,
        turnDurationMs: 24_000,
      },
    );
    const label = root.querySelector('.msg__fold-label')?.textContent;
    expect(label).toBe('已执行 24s，已调用 2 个工具');
  });

  it('多段回合 footer 时间取末段 assistant（完成时间）而非批次首条', () => {
    // final 只 patch 末段 assistant 的 timestamp；批次首条停留在回合开始时刻。
    const messages: ChatMessage[] = [
      {
        id: 'm1',
        role: 'assistant',
        content: '我先查一下。',
        timestamp: new Date(2024, 0, 15, 16, 40, 0).getTime(),
        segmentRole: 'process',
        toolCalls: [
          { toolCallId: 't1', name: 'web_search', status: 'done', startedAt: 1_700_000_000_000, duration: 100 },
        ],
      },
      {
        id: 'm2',
        role: 'assistant',
        content: '最终回答。',
        timestamp: new Date(2024, 0, 15, 16, 51, 0).getTime(),
        segmentRole: 'answer',
      },
    ];
    const root = renderAgentTurn(messages, {
      isStreaming: false,
      userPinnedOpen: null,
      turnDurationMs: 660_000,
    });
    const meta = root.querySelector('.msg__meta')?.textContent ?? '';
    expect(meta).toContain('16:51');
    expect(meta).not.toContain('16:40');
  });

  it('HTML 成果渲染为可在 Crew 浏览器打开的网站卡，并优先 index.html', () => {
    const root = renderAgentTurn(
      makeMessages({
        content: '页面已完成。',
        thinking: undefined,
        turnFileChanges: [
          {
            path: 'site/about.html',
            name: 'about.html',
            added: 10,
            removed: 0,
            status: 'added',
          },
          {
            path: 'site/index.html',
            name: 'index.html',
            added: 20,
            removed: 0,
            status: 'added',
          },
        ],
      }),
      {
        isStreaming: false,
        userPinnedOpen: null,
        turnDurationMs: 3_000,
      },
    );

    const card = root.querySelector<HTMLElement>('.msg__artifact-card');
    const open = root.querySelector<HTMLButtonElement>('.msg__artifact-open');
    expect(card?.tagName).toBe('ARTICLE');
    expect(card?.hasAttribute('data-browser-artifact')).toBe(false);
    expect(card?.getAttribute('aria-label')).toContain('index');
    expect(card?.textContent).toContain('本地 HTML · 网站');
    expect(open?.dataset.browserArtifact).toBe('site/index.html');
    // 产品名以 package.json、electron-builder appId 和 DEFAULT_HOME_DIRNAME 为准。
    expect(open?.textContent).toBe('在 Crew 打开');
    expect(open?.tagName).toBe('BUTTON');
    expect(open?.tabIndex).toBe(0);
    expect(open?.getAttribute('aria-label')).toContain('index');
    expect(root.querySelector('.msg__artifact-reveal')?.getAttribute('data-file-reveal'))
      .toBe('site/index.html');
  });

  it('本轮文件改动卡含查看入口、路径、单文件与合计红绿计数', () => {
    const root = renderAgentTurn(
      makeMessages({
        content: '已改好。',
        thinking: undefined,
        turnFileChanges: [
          {
            path: 'Crew/desktop/src/ui/chat-render.ts',
            name: 'chat-render.ts',
            added: 15,
            removed: 89,
            status: 'modified',
          },
        ],
      }),
      {
        isStreaming: false,
        userPinnedOpen: null,
        turnDurationMs: 3_000,
      },
    );
    expect(root.querySelector('.msg__file-changes')).not.toBeNull();
    expect(root.querySelector('.msg__file-changes__review')?.textContent).toBe('查看');
    expect(root.querySelector('.msg__file-changes__review')?.getAttribute('data-file-changes-path'))
      .toBe('Crew/desktop/src/ui/chat-render.ts');
    expect(root.querySelector('.msg__file-changes__row')?.getAttribute('data-file-changes-path'))
      .toBe('Crew/desktop/src/ui/chat-render.ts');
    const fileActions = root.querySelector('[data-file-reveal]');
    expect(fileActions?.getAttribute('data-file-reveal'))
      .toBe('Crew/desktop/src/ui/chat-render.ts');
    expect(fileActions?.getAttribute('aria-haspopup')).toBe('menu');
    expect(fileActions?.getAttribute('aria-expanded')).toBe('false');
    expect(root.textContent).toContain('已编辑 1 个文件');
    expect(root.querySelector('.msg__file-changes__stat--add')?.textContent).toBe('+15');
    expect(root.querySelector('.msg__file-changes__stat--del')?.textContent).toBe('-89');
    expect(root.querySelector('.msg__file-changes__badge--status')?.textContent).toBe('修改');
    expect(root.querySelector('.msg__file-changes__badge--add')?.textContent).toBe('+15');
    expect(root.querySelector('.msg__file-changes__badge--del')?.textContent).toBe('-89');
  });

  it('文件改动卡对新增/删除显示状态徽章，删除项禁用资源管理器按钮', () => {
    const root = renderAgentTurn(
      makeMessages({
        content: '清理临时文件。',
        thinking: undefined,
        turnFileChanges: [
          {
            path: 'lalala.html',
            name: 'lalala.html',
            added: 10,
            removed: 0,
            status: 'added',
          },
          {
            path: '_smoke.js',
            name: '_smoke.js',
            added: 0,
            removed: 5,
            status: 'deleted',
          },
        ],
      }),
      {
        isStreaming: false,
        userPinnedOpen: null,
        turnDurationMs: 1_000,
      },
    );
    const badges = Array.from(root.querySelectorAll('.msg__file-changes__badge--status')).map((el) => el.textContent);
    expect(badges).toEqual(['新增', '删除']);
    const deletedReveal = root.querySelector('[data-file-status="deleted"] .msg__file-changes__reveal') as HTMLButtonElement | null;
    expect(deletedReveal?.disabled).toBe(true);
    expect(deletedReveal?.getAttribute('data-file-reveal')).toBeNull();
  });

  it('过程文件与零行数二进制结果在同一文件改动卡展示', () => {
    const root = renderAgentTurn(
      makeMessages({
        content: 'PPT 已生成。',
        thinking: undefined,
        turnFileChanges: [
          { path: 'ppt/slide01.svg', name: 'slide01.svg', added: 12, removed: 0, status: 'added' },
          { path: '产品招聘看板平台介绍.pptx', name: '产品招聘看板平台介绍.pptx', added: 0, removed: 0, status: 'added', binary: true },
        ],
      }),
      { isStreaming: false, userPinnedOpen: null, turnDurationMs: 1_000 },
    );
    expect(root.textContent).toContain('已编辑 2 个文件');
    expect(root.textContent).toContain('slide01.svg');
    expect(root.textContent).toContain('产品招聘看板平台介绍.pptx');
    expect(root.querySelectorAll('.msg__file-changes')).toHaveLength(1);
    expect(root.querySelectorAll('.msg__file-changes__item')).toHaveLength(2);
  });

  it('历史回放分散在多条 assistant 的过程文件和最终结果仍合并成一张卡', () => {
    const root = renderAgentTurn(
      [
        {
          id: 'turn-process',
          role: 'assistant',
          content: '正在生成素材。',
          timestamp: 1_700_000_000_000,
          turnFileChanges: [
            { path: 'ppt/slide01.svg', name: 'slide01.svg', added: 12, removed: 0, status: 'added' },
          ],
        },
        {
          id: 'turn-result',
          role: 'assistant',
          content: 'PPT 已生成。',
          timestamp: 1_700_000_001_000,
          turnFileChanges: [
            { path: '最终结果.pptx', name: '最终结果.pptx', added: 0, removed: 0, status: 'added', binary: true },
          ],
        },
      ],
      { isStreaming: false, userPinnedOpen: null, turnDurationMs: 1_000 },
    );

    expect(root.textContent).toContain('已编辑 2 个文件');
    expect(root.textContent).toContain('slide01.svg');
    expect(root.textContent).toContain('最终结果.pptx');
    expect(root.querySelector('.msg__file-changes__item')?.textContent).toContain('最终结果.pptx');
  });

  it('文件改动卡：再显示按钮点击后并入列表并移除自身，不残留 summary', () => {
    const files = Array.from({ length: 5 }, (_, i) => ({
      path: `file-${i}.ts`,
      name: `file-${i}.ts`,
      added: i + 1,
      removed: 0,
      status: 'added' as const,
    }));
    const root = renderAgentTurn(
      makeMessages({
        content: '改了多个文件。',
        thinking: undefined,
        turnFileChanges: files,
      }),
      {
        isStreaming: false,
        userPinnedOpen: null,
        turnDurationMs: 1_000,
      },
    );
    const card = root.querySelector('.msg__file-changes') as HTMLElement;
    expect(card.querySelectorAll('.msg__file-changes__item')).toHaveLength(3);
    const moreBtn = card.querySelector('.msg__file-changes__more-btn') as HTMLButtonElement | null;
    expect(moreBtn?.textContent).toBe('再显示 2 个文件');
    expect(card.querySelector('details.msg__file-changes__more')).toBeNull();
    moreBtn?.click();
    expect(card.querySelector('.msg__file-changes__more-btn')).toBeNull();
    expect(card.querySelectorAll('.msg__file-changes__item')).toHaveLength(5);
    expect(card.textContent).not.toContain('再显示');
  });

  it('Wiki 工具的一次性确认结果渲染为消息流确认卡', () => {
    const root = renderAgentTurn(makeMessages({
      thinking: undefined,
      toolCalls: [{
        toolCallId: 'wiki-confirm-1',
        name: 'wiki_delete_pages',
        status: 'done',
        result: JSON.stringify({
          requires_confirmation: true,
          confirmation_id: 'wcf_123',
          action: 'delete_pages',
          summary: '删除 2 个页面',
          impact: { pages: 2 },
        }),
      }],
    }), { isStreaming: false, userPinnedOpen: null, turnDurationMs: 1000 });

    expect(root.querySelector('.wiki-confirmation-card')?.textContent).toContain('删除 2 个页面');
    expect(root.querySelector('[data-wiki-confirm="wcf_123"]')).not.toBeNull();
    expect(root.querySelector('[data-wiki-cancel="wcf_123"]')).not.toBeNull();
  });
});
