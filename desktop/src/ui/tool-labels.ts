/**
 * 工具调用步骤的展示名（renderer 专用，不改后端 tool name）。
 * 未知工具回退为「调用 xxx」或分段翻译常见片段。
 */

const TOOL_DISPLAY_NAMES: Record<string, string> = {
  terminal: '终端执行',
  file_read: '读文件',
  file_write: '写文件',
  glob: '查找文件',
  grep: '搜索内容',
  patch: '修改文件',
  web_search: '网页搜索',
  web_extract: '网页提取',
  vision_analyze: '图像分析',
  memory: '记忆读写',
  process: '进程管理',
  skill_view: '查看技能',
  skills_list: '技能列表',
  skills_audit: '技能审计',
  skills_repair: '技能修复',
  ask_followup_question: '追问用户',
  todo: '待办清单',
  enter_plan_mode: '进入计划模式',
  exit_plan_mode: '退出计划模式',
  delegate_to_teammate: '委派队友',
  delegate_task: '委派子任务',
  delegate_to_external_agent: '委派外部 Agent',
  run_agent: '运行子 Agent',
  collect_subagent: '收集子 Agent 结果',
  team_send_message: '团队发消息',
  team_read_messages: '读取团队消息',
  team_add_artifact: '添加团队产物',
  task_get: '查询任务',
  task_wait: '等待任务',
  task_cancel: '取消任务',
  task_list: '任务列表',
  cron_create: '创建定时任务',
  cron_list: '定时任务列表',
  cron_get: '查看定时任务',
  cron_delete: '删除定时任务',
  cron_pause: '暂停定时任务',
  cron_resume: '恢复定时任务',
  // tool_search 渐进披露的 bridge 工具（crew/tools/tool_search.py）
  tool_search: '搜索工具',
  tool_describe: '查看工具详情',
  tool_call: '调用工具',
};

const TOKEN_LABELS: Record<string, string> = {
  file: '文件',
  read: '读取',
  write: '写入',
  search: '搜索',
  list: '列表',
  get: '查询',
  create: '创建',
  delete: '删除',
  update: '更新',
  view: '查看',
  skill: '技能',
  skills: '技能',
  web: '网页',
  team: '团队',
  task: '任务',
  cron: '定时',
  memory: '记忆',
  process: '进程',
  terminal: '终端',
  delegate: '委派',
  agent: 'Agent',
  plan: '计划',
  mode: '模式',
};

/** 把 snake_case / kebab-case 工具名格式化为可读中文展示名。 */
export function formatToolDisplayName(rawName: string): string {
  const name = rawName.trim();
  if (!name) return '工具调用';
  const exact = TOOL_DISPLAY_NAMES[name];
  if (exact) return exact;
  const lower = name.toLowerCase();
  const lowerExact = TOOL_DISPLAY_NAMES[lower];
  if (lowerExact) return lowerExact;

  const parts = lower.split(/[_-]+/).filter(Boolean);
  if (parts.length === 0) return '工具调用';
  const translated = parts.map((part) => TOKEN_LABELS[part] ?? part);
  if (translated.every((part, i) => part === parts[i])) {
    return `调用 ${name.replace(/[_-]+/g, ' ')}`;
  }
  return translated.join('');
}

/* ---------- 时间线标题（对齐 web processDisplay.toolDisplayTitle） ---------- */

/** toolDisplayTitle 的最小入参：与 chat-render.ToolCallInfo 结构子集兼容。 */
export interface ToolTitleInput {
  name: string;
  uiLabel?: string | undefined;
  args?: string | undefined;
}

function parseToolArgs(args?: string): Record<string, unknown> {
  if (!args) return {};
  try {
    const value: unknown = JSON.parse(args);
    return value && typeof value === 'object' && !Array.isArray(value)
      ? value as Record<string, unknown>
      : {};
  } catch {
    return {};
  }
}

function basename(path: string): string {
  const clean = path.trim().replace(/\\/g, '/').replace(/\/+$/, '');
  return clean.split('/').filter(Boolean).pop() || clean || path;
}

function stringArg(tool: ToolTitleInput, keys: string[]): string {
  const parsed = parseToolArgs(tool.args);
  for (const key of keys) {
    const value = parsed[key];
    if (typeof value === 'string' && value.trim()) return value.trim();
  }
  return '';
}

/** bridge 工具 args.name 指向的目标工具展示名；剥掉兜底加的「调用 」前缀，避免拼出「调用 调用 xxx」。 */
function targetToolLabel(rawName: string): string {
  if (!rawName) return '';
  const label = formatToolDisplayName(rawName);
  return label.startsWith('调用 ') ? label.slice(3) : label;
}

/**
 * 过程时间线的工具标题：优先 uiLabel，其次按工具语义拼参数
 * （「运行 ls -lah」「写入 foo.ts」「搜索 pattern」），未知工具回退静态展示名。
 */
export function toolDisplayTitle(tool: ToolTitleInput): string {
  if (tool.uiLabel?.trim()) return tool.uiLabel.trim();
  const name = String(tool.name || '').trim();
  const lower = name.toLowerCase();
  const path = stringArg(tool, ['path', 'file_path']);
  const command = stringArg(tool, ['command']);
  if (['write', 'file_write'].includes(lower)) return path ? `写入 ${basename(path)}` : '写入文件';
  if (['edit', 'patch', 'apply_patch'].includes(lower)) return path ? `修改 ${basename(path)}` : '修改文件';
  if (['read', 'file_read'].includes(lower)) return path ? `读取 ${basename(path)}` : '读取文件';
  if (['bash', 'terminal', 'process'].includes(lower)) return command ? `运行 ${command}` : '运行命令';
  if (['grep', 'search_files', 'glob'].includes(lower)) {
    const query = stringArg(tool, ['query', 'pattern']);
    return query ? `搜索 ${query}` : '搜索文件';
  }
  // bridge 工具：把 args 里的 query / 目标工具名带进标题，比静态展示名更有信息量。
  if (lower === 'tool_search') {
    const query = stringArg(tool, ['query']);
    return query ? `搜索工具 ${query}` : '搜索工具';
  }
  if (lower === 'tool_describe') {
    const target = targetToolLabel(stringArg(tool, ['name']));
    return target ? `查看工具 ${target}` : '查看工具详情';
  }
  if (lower === 'tool_call') {
    const target = targetToolLabel(stringArg(tool, ['name']));
    return target ? `调用 ${target}` : '调用工具';
  }
  return formatToolDisplayName(name);
}
