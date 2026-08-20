export const FIXTURE_MARKER = '__CREW_VISUAL_FIXTURE_V1__';

export interface VisualFixtureEvent {
  readonly afterMs: number;
  readonly event: unknown;
}

export interface VisualFixtureResponse {
  readonly status?: number;
  readonly body: unknown;
}

export interface VisualFixture {
  readonly id: string;
  readonly now: number;
  readonly auth: 'anonymous' | 'authenticated';
  readonly backend: 'connected' | 'offline';
  readonly owner: {
    readonly key: string;
    readonly displayName: string;
  };
  readonly responses: Readonly<Record<string, VisualFixtureResponse>>;
  readonly events: readonly VisualFixtureEvent[];
}

const NOW = 1_735_689_600_000;
const SESSION_ID = 'fixture-session';
const OWNER = {
  key: 'fixture-owner',
  displayName: '演示用户',
} as const;

const ok = (body: unknown): VisualFixtureResponse => ({ body });

const fixtureSkills = [
  {
    name: 'document-editing',
    slug: 'document-editing',
    display_name: '文档编辑',
    description: '生成可复用的文档模板。',
    description_zh: '生成可复用的文档模板。',
    category: '办公效率',
    source: 'builtin',
  },
  {
    name: 'data-analysis',
    slug: 'data-analysis',
    display_name: '数据分析',
    description: '读取表格并提炼趋势、异常、业务结论和下一步行动建议。',
    description_zh: '读取表格并提炼趋势、异常、业务结论和下一步行动建议。',
    category: '数据研究',
    source: 'builtin',
  },
  {
    name: 'meeting-notes',
    slug: 'meeting-notes',
    display_name: '会议纪要',
    description: '从记录中提取决策、行动项与负责人。',
    description_zh: '从记录中提取决策、行动项与负责人。',
    category: '协作沟通',
    source: 'user',
  },
  {
    name: 'travel-planning',
    slug: 'travel-planning',
    display_name: '差旅规划',
    description: '组合行程、交通与住宿信息。',
    description_zh: '组合行程、交通与住宿信息。',
    category: '商旅服务',
    source: 'builtin',
  },
] as const;

const fixtureExperts = [
  ['product-strategy', '产品战略专家', '梳理产品方向、优先级与路线图。', ['产品', '战略']],
  ['software-architecture', '软件架构专家', '评审系统边界、可维护性与演进风险。', ['架构', '研发']],
  ['experience-design', '体验设计专家', '优化复杂工作流的信息架构和交互。', ['设计', 'UX']],
  ['security-review', '安全评审专家', '识别权限、数据和执行环境风险。', ['安全', '威胁建模']],
  ['academic-research', '学术研究专家', '组织文献证据并形成研究框架。', ['学术', '研究']],
  ['data-insight', '数据洞察专家', '从业务数据中发现趋势与异常。', ['数据', '分析']],
  ['delivery-coach', '交付教练', '推动跨团队计划、决策和复盘。', ['通用', '协作']],
].map(([id, name, description, tags], index) => ({
  id,
  name,
  description,
  avatar: '',
  tags,
  is_default: index < 2,
  sort_order: index,
  summoned: index === 0,
  sample_prompts: [`请以${name}的视角评审当前方案`],
}));

const commonResponses = {
  '/api/fixture/state': ok({
    owner_key: OWNER.key,
    workspace: { id: 'fixture-workspace', name: '产品演示空间' },
  }),
  '/api/config': ok({
    model: 'fixture-model',
    has_key: true,
    base_url: '',
    active_model_id: 'fixture-model',
    models: [{
      id: 'fixture-model',
      name: '演示模型',
      model: 'fixture-model',
      has_key: true,
      loaded: true,
      builtin: true,
    }],
  }),
  '/api/sessions': ok([]),
  '/api/sessions/status': ok({}),
  '/api/workspaces': ok([{
    id: 'fixture-workspace',
    name: '产品演示空间',
    description: '仅用于视觉验收的固定工作空间',
    instructions: '',
  }]),
  '/api/channel-sessions': ok({ platforms: [] }),
  '/api/scenarios': ok([{
    id: 'fixture-scenario',
    title: '整理本周工作',
    description: '汇总进展并生成下一步行动项',
    items: [{
      id: 'fixture-weekly-review',
      title: '生成本周复盘',
      query: '请整理本周工作进展',
    }],
  }]),
  '/api/scenarios/intro-lines': ok(['今天想先处理哪项工作？']),
  '/api/scenarios/loading-status': ok(['正在整理上下文']),
  '/api/skills': ok(fixtureSkills),
  '/api/skills/store': ok({
    installed: fixtureSkills,
    optional: [{
      name: 'contract-review',
      slug: 'contract-review',
      display_name: '合同审阅',
      description: '检查条款风险并生成问题清单。',
      description_zh: '检查条款风险并生成问题清单。',
      category: '法务支持',
      source: 'optional',
    }],
  }),
  '/api/plugins': ok([
    {
      name: 'feishu', key: 'feishu', label: '飞书协作', version: '2.1.0',
      description: '连接飞书消息、文档与审批。', kind: 'channel', enabled: true,
      installed: true, system_allowed: true, role_allowed: true, user_enabled: true,
      user_enabled_explicit: true, effective_enabled: true, toggle_endpoint: '/enabled',
      tools: ['send_message'], hooks: [], platforms: ['feishu'],
    },
    {
      name: 'browser', key: 'browser', label: '浏览器控制', version: '1.2.0',
      description: '提供受控网页浏览与自动化能力。', kind: 'tool', enabled: false,
      installed: true, system_allowed: true, role_allowed: false, user_enabled: false,
      user_enabled_explicit: false, effective_enabled: false, toggle_endpoint: '/enabled',
      tools: ['browser_open'], hooks: [], platforms: [],
    },
  ]),
  '/api/experts': ok(fixtureExperts),
  '/api/experts/summoned': ok(fixtureExperts.slice(0, 1)),
  '/api/expert-teams': ok([{
    id: 'product-council',
    name: '产品决策委员会',
    description: '由产品、数据与技术专家共同评审复杂决策。',
    avatar: '',
    tags: ['产品', '数据', '研发'],
    sort_order: 0,
    mode: 'dynamic_kanban',
    members: {
      product: 'product-strategy',
      data: 'data-insight',
      engineering: 'software-architecture',
    },
    summoned: false,
    sample_prompts: ['评审这份产品路线图并给出统一结论'],
  }]),
  '/api/expert-teams/summoned': ok([]),
  '/api/runtimes': ok([]),
  '/api/external-agents': ok([]),
  '/api/external-teams': ok([]),
  '/api/external-teams/roles': ok([]),
  '/api/wiki/kbs': ok({
    ok: true,
    kbs: [{ id: 'default', name: '产品知识库', created_at: NOW - 86_400, updated_at: NOW }],
  }),
  '/api/wiki/pages': ok({
    ok: true,
    pages: [{
      id: 'fixture-wiki-page',
      page_type: 'topic',
      title: '桌面端设计规范',
      summary: '统一页面结构、组件状态与可访问性约束。',
      file_path: '设计/desktop-design-system.md',
      sources: [],
      related: [],
      tags: ['设计系统', '桌面端'],
      aliases: [],
      created_at: NOW - 86_400,
      updated_at: NOW,
    }],
    source_titles: {},
    source_files: {},
  }),
  '/api/wiki/agent-session': ok({
    ok: true,
    session_id: 'fixture-wiki-agent',
    kb_id: 'default',
  }),
  '/api/session/fixture-wiki-agent': ok([]),
  '/api/session/fixture-wiki-agent/todos': ok({ todos: [] }),
  '/api/usage': ok({
    total_tokens: 182_430,
    prompt_tokens: 121_230,
    completion_tokens: 61_200,
    total_cost: 0,
    sessions: 12,
  }),
  '/api/runtime/concurrency': ok({
    max_active_runs: 4,
    global_active: 2,
    global_queued: 1,
  }),
  '/api/system/metrics': ok({
    uptime_s: 132_300,
    cpu_count: 12,
    cpu_percent: 36,
    memory: { total_gb: 32, used_gb: 13.4, percent: 42 },
    disk: { total_gb: 512, used_gb: 238, free_gb: 274, percent: 46 },
    network: { bytes_sent: 28_300_000, bytes_recv: 91_700_000 },
    process: { rss_mb: 286, pid: 12_580 },
  }),
  '/api/platforms': ok([
    {
      name: 'feishu',
      label: '飞书',
      available: true,
      configured: true,
      connected: true,
      live_connected: true,
    },
  ]),
  '/api/mcp/servers': ok({
    ok: true,
    servers: [
      {
        name: 'filesystem',
        transport: 'stdio',
        connected: true,
        error: '',
        tools: ['read_file', 'list_directory'],
        config: {
          transport: 'stdio',
          command: 'node',
          args: ['filesystem-server.js'],
        },
      },
      {
        name: 'broken-stdio',
        transport: 'stdio',
        connected: false,
        error: 'MCP stdio host process is disabled; use a managed transport or explicitly approved host configuration',
        tools: [],
        config: {
          transport: 'stdio',
          command: 'node',
          args: ['broken-server.js'],
        },
      },
    ],
  }),
  '/api/mcp/cua-driver/status': ok({
    ok: true,
    installed: true,
    binary: 'cua-driver',
    version: '1.0.0',
    daemon_running: true,
    mcp_enabled: true,
    tools_registered: ['computer_use'],
  }),
  '/api/system/logs': ok({
    total: 2,
    items: [
      {
        ts: NOW / 1000 - 12,
        level: 'INFO',
        name: 'crew.gateway',
        message: 'Desktop visual fixture connected',
      },
      {
        ts: NOW / 1000 - 4,
        level: 'WARNING',
        name: 'crew.runtime',
        message: '视觉验收日志：等待一个排队任务',
      },
    ],
  }),
} satisfies Record<string, VisualFixtureResponse>;

function sessionResponses(
  title: string,
  history: readonly unknown[],
  extras: {
    plan?: string;
    tasks?: readonly unknown[];
    todos?: readonly unknown[];
  } = {},
): Record<string, VisualFixtureResponse> {
  return {
    ...commonResponses,
    '/api/sessions': ok([{
      session_id: SESSION_ID,
      title,
      message_count: history.length,
      updated_at: NOW,
      created_at: NOW - 60_000,
      workspace_id: 'fixture-workspace',
      model_profile_id: 'fixture-model',
      model_label: '演示模型',
    }]),
    '/api/sessions/status': ok({ [SESSION_ID]: 'idle' }),
    [`/api/session/${SESSION_ID}`]: ok(history),
    [`/api/session/${SESSION_ID}/status`]: ok({
      session_id: SESSION_ID,
      live: 'idle',
      active_request_id: null,
      queue_depth: 0,
      last_status: 'idle',
      last_error: '',
    }),
    [`/api/session/${SESSION_ID}/model`]: ok({
      ok: true,
      source: 'crew',
      model_profile_id: 'fixture-model',
      model_label: '演示模型',
      models: [],
      model_switchable: true,
    }),
    [`/api/session/${SESSION_ID}/context`]: ok({
      available: true,
      used_tokens: 4_096,
      max_tokens: 32_768,
      ratio: 0.125,
      source: 'provider',
    }),
    [`/api/session/${SESSION_ID}/plan`]: ok({
      active: Boolean(extras.plan),
      plan: extras.plan || '',
      plan_file: 'plans/fixture-plan.md',
      status: extras.plan ? 'pending' : 'idle',
    }),
    [`/api/session/${SESSION_ID}/todos`]: ok({ todos: extras.todos || [] }),
    [`/api/session/${SESSION_ID}/expert`]: ok({ expert_id: null }),
    [`/api/session/${SESSION_ID}/expert-team`]: ok({ expert_team_id: null }),
    '/api/tasks': ok(extras.tasks || []),
  };
}

function sessionHistoryResponses(): Record<string, VisualFixtureResponse> {
  return {
    ...sessionResponses('季度复盘材料整理', [{
      role: 'assistant',
      content: '已整理季度复盘材料，并标记了需要确认的数据。',
      timestamp: NOW - 30_000,
    }]),
    '/api/workspaces': ok([
      {
        id: 'fixture-workspace',
        name: '产品演示空间',
        description: '仅用于视觉验收的固定工作空间',
        instructions: '',
      },
      {
        id: 'finance-workspace',
        name: '财务资料',
        description: '预算与报表工作空间',
        instructions: '',
      },
    ]),
    '/api/sessions': ok([
      {
        session_id: SESSION_ID,
        title: '季度复盘材料整理',
        message_count: 4,
        updated_at: NOW / 1000,
        created_at: NOW / 1000 - 3_600,
        workspace_id: 'fixture-workspace',
        pinned: true,
        model_profile_id: 'fixture-model',
        model_label: '演示模型',
      },
      {
        session_id: 'fixture-budget',
        title: '核对年度预算差异',
        message_count: 7,
        updated_at: NOW / 1000 - 240,
        created_at: NOW / 1000 - 7_200,
        workspace_id: 'finance-workspace',
      },
      {
        session_id: 'fixture-running',
        title: '汇总本周项目进展',
        message_count: 3,
        updated_at: NOW / 1000 - 90,
        created_at: NOW / 1000 - 1_800,
        workspace_id: 'default',
      },
      {
        session_id: 'fixture-error',
        title: '生成客户回访清单',
        message_count: 2,
        updated_at: NOW / 1000 - 600,
        created_at: NOW / 1000 - 3_600,
        workspace_id: 'default',
      },
      {
        session_id: 'fixture-archived',
        title: '旧版发布说明',
        message_count: 5,
        updated_at: NOW / 1000 - 86_400,
        created_at: NOW / 1000 - 172_800,
        workspace_id: 'default',
        archived: true,
      },
    ]),
    '/api/sessions/status': ok({
      [SESSION_ID]: 'idle',
      'fixture-budget': 'idle',
      'fixture-running': 'running',
      'fixture-error': 'error',
      'fixture-archived': 'idle',
      'agent:main:feishu:fixture-user': 'queued',
    }),
    '/api/channel-sessions': ok({
      platforms: [{
        platform: 'feishu',
        label: '飞书',
        sessions: [{
          session_id: 'agent:main:feishu:fixture-user',
          title: '飞书项目同步',
          updated_at: NOW / 1000 - 120,
          workspace_id: 'default',
        }],
      }],
    }),
  };
}

function composerResponses(): Record<string, VisualFixtureResponse> {
  return {
    ...sessionResponses('Composer 状态验收', [{
      role: 'user',
      content: '请把本周进展整理成三条结论。',
      timestamp: NOW - 10_000,
    }]),
    '/api/sessions/status': ok({ [SESSION_ID]: 'running' }),
    [`/api/session/${SESSION_ID}/status`]: ok({
      session_id: SESSION_ID,
      live: 'running',
      active_request_id: 'fixture-composer-request',
      queue_depth: 0,
      last_status: 'running',
      last_error: '',
    }),
  };
}

function workspaceNavigationResponses(): Record<string, VisualFixtureResponse> {
  return {
    ...sessionHistoryResponses(),
    '/api/workspaces': ok([
      {
        id: 'default',
        name: '对话',
        description: '默认工作空间',
        instructions: '',
      },
      {
        id: 'fixture-workspace',
        name: '产品演示空间',
        description: '仅用于视觉验收的固定工作空间',
        instructions: '遵循项目约定并优先运行现有检查。',
        root_path: '/visual-fixtures/Product',
      },
      {
        id: 'finance-workspace',
        name: '财务资料',
        description: '预算与报表工作空间',
        instructions: '修改报表前先确认版本。',
        root_path: '/visual-fixtures/Missing/Finance',
      },
      {
        id: 'archive-workspace',
        name: '历史归档',
        description: '只读历史材料',
        instructions: '',
        root_path: '/visual-fixtures/Archive',
        hidden: true,
      },
    ]),
  };
}

const openEvent = {
  afterMs: 0,
  event: { type: 'open' },
} as const;

function gatewayMessage(afterMs: number, frame: unknown): VisualFixtureEvent {
  return {
    afterMs,
    event: {
      type: 'message',
      data: JSON.stringify(frame),
    },
  };
}

function workResponses(): Record<string, VisualFixtureResponse> {
  const todayItem = {
    item_id: 'fixture-work-item',
    owner_account_id: OWNER.key,
    title: '准备季度经营复盘',
    description: '汇总经营数据并形成会议材料',
    category: '经营分析',
    related_system: 'portal',
    workspace_id: 'fixture-workspace',
    processing_session_id: 'fixture-linked-session',
    business_status: 'in_progress',
    execution_status: 'running',
    sync_status: 'not_applicable',
    priority: 'high',
    disposition: 'active',
    source: null,
    due_at: NOW + 3_600_000,
    version: 2,
    created_at: NOW - 86_400_000,
    updated_at: NOW - 120_000,
  };
  const pendingItem = {
    ...todayItem,
    item_id: 'fixture-pending-item',
    processing_session_id: null,
    title: '确认客户回访安排',
    description: '确认本周重点客户、回访负责人和沟通时间',
    category: '客户沟通',
    related_system: 'todo',
    business_status: 'pending_confirmation',
    execution_status: 'not_started',
    priority: 'medium',
    version: 1,
    updated_at: NOW - 300_000,
  };
  const tomorrowItem = {
    ...todayItem,
    item_id: 'fixture-mail-item',
    processing_session_id: null,
    title: '回复合作方方案邮件',
    description: '核对报价与交付边界后发送正式回复',
    category: '邮件',
    related_system: 'mail',
    business_status: 'pending',
    execution_status: 'not_started',
    priority: 'medium',
    due_at: NOW + 86_400_000,
    version: 1,
    updated_at: NOW - 600_000,
  };
  const meetingItem = {
    ...todayItem,
    item_id: 'fixture-meeting-item',
    processing_session_id: 'fixture-meeting-linked-session',
    title: '准备产品例会',
    description: '整理本周进展、风险和需要决策的问题',
    category: '会议',
    related_system: 'meeting',
    priority: 'medium',
    due_at: NOW + 172_800_000,
    updated_at: NOW - 900_000,
  };
  const completedItem = {
    ...todayItem,
    item_id: 'fixture-completed-item',
    processing_session_id: null,
    title: '提交上周工作总结',
    description: '已完成并沉淀到个人知识',
    category: '工作总结',
    related_system: 'todo',
    business_status: 'completed',
    execution_status: 'succeeded',
    priority: 'low',
    due_at: NOW - 86_400_000,
    version: 4,
    updated_at: NOW - 43_200_000,
  };
  const archivedItem = {
    ...completedItem,
    item_id: 'fixture-archived-item',
    title: '归档年度培训资料',
    description: '培训已结束，材料已归档',
    related_system: 'hr',
    disposition: 'archived',
    due_at: NOW - 604_800_000,
    version: 5,
    updated_at: NOW - 345_600_000,
  };
  const items = [
    todayItem,
    pendingItem,
    tomorrowItem,
    meetingItem,
    completedItem,
    archivedItem,
  ];
  const createdItem = {
    ...todayItem,
    item_id: 'fixture-created-item',
    processing_session_id: null,
    title: '新建演示事项',
    description: '通过新建事项表单创建',
    related_system: 'calendar',
    business_status: 'pending',
    execution_status: 'not_started',
    priority: 'medium',
    due_at: NOW + 259_200_000,
    version: 1,
    created_at: NOW,
    updated_at: NOW,
  };
  const itemHistory = items.map((item) => ({
    id: `work_item:${item.item_id}`,
    entity_type: 'work_item',
    session_id: null,
    title: item.title,
    workspace_id: item.workspace_id,
    updated_at: item.updated_at,
    work_item_id: item.item_id,
    archived: item.disposition === 'archived',
    pinned: false,
    read_only: false,
    open_mode: 'work',
  }));
  const templates = [
    ['data-analysis', '数据分析', '导入 CSV / Excel 并生成结论', 'analysis'],
    ['mail-assistant', '邮件助手', '撰写、回复并检查邮件', 'mail'],
    ['contract-review', '合同审阅', '提取风险、期限与待确认条款', 'document'],
    ['calendar', '日程管理', '整理会议、提醒和时间冲突', 'calendar'],
    ['research', '信息检索', '检索公开资料并给出来源', 'research'],
    ['finance', '财务助手', '核对预算、报销和票据', 'finance'],
    ['travel', '差旅服务', '整理机票、酒店与行程', 'travel'],
    ['project', '项目管理', '拆解任务、跟进进度与风险', 'project'],
  ].map(([templateId, name, description, category], index) => ({
    owner_account_id: OWNER.key,
    template_id: templateId,
    source: 'system',
    name,
    description,
    category,
    blueprint: {},
    version: 1,
    usage_count: 12 - index,
    last_used_at: NOW / 1000 - index * 86_400,
    created_at: 0,
    updated_at: 0,
  }));
  const dashboardBrief = {
    brief_id: 'fixture-brief',
    business_date: '2026-07-28',
    workspace_id: null,
    content: {
      summary: '今天优先完成经营复盘，并确认客户回访安排。',
      today_items: [todayItem, pendingItem, tomorrowItem],
      focus_items: [todayItem],
      overdue_items: [],
      meeting_items: [meetingItem],
      mail_items: [tomorrowItem],
      pending_confirmations: [pendingItem],
      execution_items: [todayItem, meetingItem],
    },
    version: 3,
    archived: false,
    created_at: NOW - 3_600_000,
    updated_at: NOW - 60_000,
  };
  return {
    ...sessionResponses('季度经营复盘', []),
    '/api/work/history': ok({
      entries: [
        {
          id: `work_session:${SESSION_ID}`,
          entity_type: 'work_session',
          session_id: SESSION_ID,
          title: '季度经营复盘',
          workspace_id: 'fixture-workspace',
          updated_at: NOW,
          work_item_id: null,
          archived: false,
          pinned: true,
          read_only: false,
          open_mode: 'work',
        },
        {
          id: 'work_session:fixture-mail-session',
          entity_type: 'work_session',
          session_id: 'fixture-mail-session',
          title: '起草合作方邮件',
          workspace_id: 'fixture-workspace',
          updated_at: NOW - 180_000,
          work_item_id: null,
          archived: false,
          pinned: false,
          read_only: false,
          open_mode: 'work',
        },
        ...itemHistory,
        {
          id: 'agent_session:fixture-agent-session',
          entity_type: 'agent_session',
          session_id: 'fixture-agent-session',
          title: '通用助手资料检索',
          workspace_id: 'fixture-workspace',
          updated_at: NOW - 240_000,
          work_item_id: null,
          archived: false,
          pinned: false,
          read_only: true,
          open_mode: 'assistant',
        },
      ],
      count: itemHistory.length + 3,
    }),
    '/api/work/items': ok({ items, count: items.length }),
    'POST /api/work/items': { status: 201, body: createdItem },
    'POST /api/work/items/fixture-created-item/processing-session': ok({
      ...createdItem,
      processing_session_id: 'fixture-created-session',
      version: createdItem.version + 1,
      updated_at: NOW,
    }),
    '/api/work/items/fixture-created-item/activity': ok({ events: [], count: 0 }),
    ...Object.fromEntries(items.flatMap((item) => [
      [`/api/work/items/${item.item_id}`, ok(item)],
      [`PATCH /api/work/items/${item.item_id}`, ok({
        ...item,
        business_status: 'in_progress',
        version: item.version + 1,
        updated_at: NOW,
      })],
      [`/api/work/items/${item.item_id}/activity`, ok({
        events: [{
          event_id: `event-${item.item_id}`,
          owner_account_id: OWNER.key,
          item_id: item.item_id,
          event_type: 'created',
          actor: 'user',
          before_state: null,
          after_state: { business_status: item.business_status, version: 1 },
          created_at: item.created_at,
        }],
        count: 1,
      })],
      [`POST /api/work/items/${item.item_id}/actions`, ok({
        ...item,
        business_status: 'completed',
        execution_status: 'succeeded',
        version: item.version + 1,
        updated_at: NOW,
      })],
    ])),
    'POST /api/work/items/fixture-pending-item/actions': ok({
      ...pendingItem,
      disposition: 'archived',
      version: pendingItem.version + 1,
      updated_at: NOW,
    }),
    '/api/work/dashboard': ok({ brief: dashboardBrief }),
    'POST /api/work/dashboard/refresh': ok({
      brief: { ...dashboardBrief, version: 4, updated_at: NOW },
    }),
    '/api/work/reports': ok({
      report: {
        report_id: null,
        period: 'day',
        period_start: '2025-01-01',
        period_end: '2025-01-01',
        workspace_id: null,
        metrics: {
          created: 6,
          completed: 2,
          in_progress: 2,
          overdue: 1,
          completion_rate: 0.3333,
          status_counts: { pending: 2, in_progress: 2, completed: 2 },
          category_counts: { 经营分析: 2, 会议: 1, 客户沟通: 1, 邮件: 1, 工作总结: 1 },
        },
        archived: false,
        generated_at: NOW / 1000,
        archived_at: null,
      },
    }),
    'POST /api/work/reports/archive': ok({
      report: {
        report_id: 'fixture-report',
        period: 'day',
        period_start: '2025-01-01',
        period_end: '2025-01-01',
        workspace_id: null,
        metrics: {
          created: 6,
          completed: 2,
          in_progress: 2,
          overdue: 1,
          completion_rate: 0.3333,
          status_counts: { pending: 2, in_progress: 2, completed: 2 },
          category_counts: { 经营分析: 2, 会议: 1, 客户沟通: 1, 邮件: 1, 工作总结: 1 },
        },
        archived: true,
        generated_at: NOW / 1000,
        archived_at: NOW / 1000,
      },
    }),
    '/api/work/sources': ok({
      items: [{
        owner_account_id: OWNER.key,
        connector_key: 'organization-mail',
        enabled: false,
        status: 'unavailable',
        last_error: '组织尚未配置邮件连接器',
        last_synced_at: null,
        updated_at: NOW,
      }],
      count: 1,
    }),
    '/api/work/sources/records': ok({ items: [], count: 0 }),
    '/api/work/templates': ok({ items: templates, count: templates.length }),
    '/api/work/preferences/settings': ok({ auto_learning_enabled: true }),
    '/api/work/preferences': ok({
      items: [{
        owner_account_id: OWNER.key,
        preference_id: 'fixture-preference',
        category: 'presentation',
        content: '经营汇报先给结论并保留原文件',
        scope: 'global',
        scope_id: null,
        status: 'active',
        auto_enabled: true,
        evidence_session_count: 3,
        version: 2,
        created_at: NOW / 1000 - 172_800,
        updated_at: NOW / 1000 - 86_400,
      }],
      count: 1,
    }),
    '/api/work/settings': ok({
      dnd_enabled: false,
      dnd_start: '22:00',
      dnd_end: '07:00',
      auto_status_transition: true,
    }),
    'PUT /api/work/settings': ok({
      dnd_enabled: false,
      dnd_start: '22:00',
      dnd_end: '07:00',
      auto_status_transition: true,
    }),
    '/api/work/knowledge/personal': ok({
      items: [{
        id: 'knowledge-quarterly-review',
        page_type: 'work_result',
        title: '上季度经营复盘',
        summary: '已确认的复盘结论与后续行动。',
      }],
      count: 1,
    }),
    '/api/work/knowledge/organization': ok({
      items: [{
        id: 'knowledge-org-template',
        page_type: 'organization',
        title: '经营复盘规范',
        summary: '组织发布的复盘结构与指标口径。',
      }],
      count: 1,
      available: true,
    }),
    '/api/work/workspaces/default/index': ok({
      enabled: false,
      state: 'idle',
      updated_at: NOW,
    }),
    '/api/work/sessions': ok({
      session_id: SESSION_ID,
      title: '整理季度材料并给出下一步',
      workspace_id: 'default',
      product_mode: 'work',
    }),
    // 办公动态快照（与 crew/api/{mail,todo,schedule,meeting}/latest 形态一致）
    '/api/mail/latest': ok({
      ok: true,
      data: {
        count: 1,
        results: [{
          subject: '关于季度经营复盘的资料',
          from: '王一寒<wangyi@crew.example>',
          sendDate: '2026-07-28 16:45:51',
          summary: '请确认复盘数据与结论。',
          mid: 'fixture-mail-1',
          read: false,
          readStatus: '未读',
        }],
      },
      fetched_at: NOW / 1000 - 120,
      stale: false,
      error: null,
    }),
    'POST /api/mail/detail': ok({
      ok: true,
      subject: '关于季度经营复盘的资料',
      from: '王一寒 <wangyi@crew.example>',
      content: [
        '<p>林悦，你好：</p>',
        '<p>季度经营复盘所需的收入、客户与交付数据已经整理完成，请重点核对附件中的口径差异。</p>',
        '<p>确认后请在周五前回传最终版汇报材料。</p>',
        '<p>谢谢。<br>王一寒</p>',
      ].join(''),
    }),
    '/api/todo/latest': ok({
      ok: true,
      data: {
        summary: '您有 1 项公文待办，其中 1 项逾期待办，请尽快处理。',
        groups: [{
          groupName: '公文待办',
          count: 1,
          dataList: [{
            itemTitle: '关于…的通知',
            systemName: '综合办公平台',
            drafterName: '张三',
            itemCreateTime: '2026-07-27',
            url: 'https://example.com/todo/1',
          }],
        }],
        counts: [],
      },
      fetched_at: NOW / 1000 - 120,
      stale: false,
      error: null,
    }),
    '/api/schedule/latest': ok({
      ok: true,
      data: {
        total: 1,
        pages: 1,
        count: 1,
        results: [{
          scheduleId: 'fixture-sch-1',
          scheduleTheme: '产品例会',
          scheduleStartDate: '2026-07-28',
          scheduleStartTime: '10:00:00',
          scheduleEndDate: '2026-07-28',
          scheduleEndTime: '11:00:00',
        }],
      },
      fetched_at: NOW / 1000 - 120,
      stale: false,
      error: null,
    }),
    '/api/meeting/latest': ok({
      ok: true,
      data: {
        wait_count: 1,
        meetings: [{
          infoId: 2174,
          infoName: '产品决策会',
          status: 1,
          time: '2026-07-28 10:00:00',
          conferenceTypeName: '公司决策会',
          url: 'https://example.com/meeting/2174',
        }],
      },
      fetched_at: NOW / 1000 - 120,
      stale: false,
      error: null,
    }),
  };
}

const welcomeFixture: VisualFixture = {
  id: 'chat-welcome',
  now: NOW,
  auth: 'authenticated',
  backend: 'connected',
  owner: OWNER,
  responses: commonResponses,
  events: [openEvent],
};

const fixtures = {
  baseline: {
    ...welcomeFixture,
    id: 'baseline',
  },
  'shell-empty': {
    id: 'shell-empty',
    now: NOW,
    auth: 'anonymous',
    backend: 'offline',
    owner: OWNER,
    responses: commonResponses,
    events: [],
  },
  'chat-welcome': welcomeFixture,
  'session-history': {
    id: 'session-history',
    now: NOW,
    auth: 'authenticated',
    backend: 'connected',
    owner: OWNER,
    responses: sessionHistoryResponses(),
    events: [openEvent],
  },
  'workspace-navigation': {
    id: 'workspace-navigation',
    now: NOW,
    auth: 'authenticated',
    backend: 'connected',
    owner: OWNER,
    responses: workspaceNavigationResponses(),
    events: [openEvent],
  },
  'composer-states': {
    id: 'composer-states',
    now: NOW,
    auth: 'authenticated',
    backend: 'connected',
    owner: OWNER,
    responses: composerResponses(),
    events: [openEvent],
  },
  'agent-runtimes': {
    id: 'agent-runtimes',
    now: NOW,
    auth: 'authenticated',
    backend: 'connected',
    owner: OWNER,
    responses: {
      ...commonResponses,
      '/api/config': ok({
        model: 'fixture-model',
        has_key: true,
        base_url: '',
        active_model_id: 'fixture-model',
        models: [{
          id: 'fixture-model', name: '演示模型', model: 'fixture-model',
          has_key: true, loaded: true, builtin: true,
        }],
        external_agents: { enabled: true },
      }),
      '/api/runtimes': ok([
        {
          id: 'runtime-kimi', name: 'Kimi', provider: 'kimi', version: '0.26.0',
          availability_status: 'ready', executable_path: '/fixture/bin/kimi',
          metadata: { runtime_profile_version: 1 },
        },
        {
          id: 'runtime-codex', name: 'Codex', provider: 'codex', version: 'codex-cli 0.147.0-alpha.1.2',
          availability_status: 'degraded', executable_path: '/fixture/bin/codex',
          metadata: {
            runtime_profile_version: 1,
            probe: { error_code: 'probe_failed', message: '模型服务暂时未响应' },
          },
        },
        {
          id: 'runtime-hermes', name: 'Hermes', provider: 'hermes', version: 'Hermes Agent v0.16.0 (2026.6.5)',
          availability_status: 'degraded', executable_path: '/fixture/bin/hermes',
          metadata: {
            runtime_profile_version: 1,
            probe: { error_code: 'models_empty', message: '运行时未返回可选模型' },
          },
        },
      ]),
      'DELETE /api/runtimes/runtime-kimi': ok({ ok: true }),
      'DELETE /api/runtimes/runtime-codex': ok({ ok: true }),
      'DELETE /api/runtimes/runtime-hermes': ok({ ok: true }),
    },
    events: [openEvent],
  },
  'work-dashboard': {
    id: 'work-dashboard',
    now: NOW,
    auth: 'authenticated',
    backend: 'connected',
    owner: OWNER,
    responses: workResponses(),
    events: [openEvent],
  },
  'chat-streaming': {
    id: 'chat-streaming',
    now: NOW,
    auth: 'authenticated',
    backend: 'connected',
    owner: OWNER,
    responses: sessionResponses('流式输出验收', [{
      role: 'user',
      content: '请把本周进展整理成三条结论。',
      timestamp: NOW - 10_000,
    }]),
    events: [
      openEvent,
      gatewayMessage(600, {
        kind: 'delta',
        body: { text: '正在汇总：第一项已完成，第二项正在推进。' },
        is_final: false,
        sequence: 1,
        gateway_sequence: 1,
        request_id: 'fixture-request',
        session_id: SESSION_ID,
      }),
    ],
  },
  'inspectors-all': {
    id: 'inspectors-all',
    now: NOW,
    auth: 'authenticated',
    backend: 'connected',
    owner: OWNER,
    responses: sessionResponses(
      'Inspector 状态验收',
      [
        {
          role: 'user',
          content: '请更新验收说明。',
          timestamp: NOW - 30_000,
        },
        {
          role: 'assistant',
          content: '已更新说明，并补充了验证步骤。',
          timestamp: NOW - 20_000,
          turn_file_changes: [{
            path: 'docs/frontend/acceptance.md',
            name: 'acceptance.md',
            added: 18,
            removed: 4,
            status: 'modified',
          }],
        },
      ],
      {
        plan: '# 验收计划\n\n1. 核对上下文\n2. 检查文件差异\n3. 完成视觉验证',
        todos: [
          { id: 'todo-1', content: '核对上下文', status: 'completed' },
          { id: 'todo-2', content: '检查文件差异', status: 'in_progress' },
        ],
        tasks: [
          {
            id: 'task-1',
            title: '检查文件差异',
            status: 'running',
            session_id: SESSION_ID,
          },
        ],
      },
    ),
    events: [openEvent],
  },
  'edge-content': {
    id: 'edge-content',
    now: NOW,
    auth: 'authenticated',
    backend: 'connected',
    owner: OWNER,
    responses: sessionResponses(
      `极端内容 ${'很长的会话标题'.repeat(32)}`,
      [
        {
          role: 'user',
          content: `请检查这个超长标识：${'CrewLongIdentifier'.repeat(24)}`,
          timestamp: NOW - 20_000,
        },
        {
          role: 'assistant',
          content: [
            '## 长内容验收',
            '',
            '| 项目 | 状态 | 说明 |',
            '| --- | --- | --- |',
            ...Array.from({ length: 18 }, (_, index) =>
              `| 条目 ${index + 1} | 处理中 | 这是用于验证滚动、换行和容器边界的固定内容。 |`),
            '',
            '```text',
            'visible markdown content',
            '```',
          ].join('\n'),
          timestamp: NOW - 10_000,
        },
      ],
    ),
    events: [openEvent],
  },
  'edge-error': {
    id: 'edge-error',
    now: NOW,
    auth: 'authenticated',
    backend: 'connected',
    owner: OWNER,
    responses: sessionResponses('错误状态验收', [{
      role: 'assistant',
      content: '执行失败：无法读取所需数据。请重试或查看运行日志。',
      event_type: 'error',
      timestamp: NOW - 10_000,
    }]),
    events: [openEvent],
  },
  'edge-forbidden': {
    id: 'edge-forbidden',
    now: NOW,
    auth: 'authenticated',
    backend: 'connected',
    owner: OWNER,
    responses: sessionResponses('权限状态验收', [{
      role: 'system',
      content: '当前账号无权查看此内容。请联系管理员申请访问权限。',
      event_type: 'error',
      timestamp: NOW - 10_000,
    }]),
    events: [openEvent],
  },
} as const satisfies Record<string, VisualFixture>;

export function getFixture(id: string): VisualFixture {
  const fixture = fixtures[id as keyof typeof fixtures];
  if (!fixture) throw new Error(`Unknown visual fixture: ${id}`);
  return fixture;
}

export function selectFixture(search: string = window.location.search): VisualFixture {
  const id = new URLSearchParams(search).get('fixture') || 'baseline';
  return getFixture(id);
}
