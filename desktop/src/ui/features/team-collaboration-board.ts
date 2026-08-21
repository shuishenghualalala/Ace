/**
 * 外部 Team Session 的「协作」看板。
 *
 * 视觉与信息结构逐项对齐 Web TaskBoard，但保持 Desktop 原生 TypeScript DOM：
 * 不引入 React，不挂载 Web 组件，不创建第二套后端协议。
 */

import {
  backendApi,
  type ExternalAgent,
  type ExternalTeam,
  type RuntimeModelProfile,
  type Task,
  type TeamMemberModelBinding,
} from '../backend-client';
import type { ChatMessage } from '../chat-render';
import { escapeHtml, notify, state } from '../state';
import { setRuntimeStyle } from '../components/runtime-style';

const CREW_BUILTIN_AGENT_ID = 'crew::builtin';
const TEAM_PLAN_SOURCES = new Set(['team_plan', 'team_kanban', 'team_flow_fallback']);
const REFRESH_MS = 2_000;

export type TeamFlowStatus = 'pending' | 'running' | 'blocked' | 'completed' | 'failed' | 'cancelled';

export interface TeamFlowNode {
  id: string;
  title: string;
  fullTitle: string;
  owner: string;
  agents: string[];
  status: TeamFlowStatus;
  summary: string;
  summaryItems: string[];
  raw: Task;
}

export interface TeamFlowStage {
  id: string;
  depth: number;
  nodes: TeamFlowNode[];
}

export interface TeamFlowTurn {
  id: string;
  title: string;
  stages: TeamFlowStage[];
  status: TeamFlowStatus;
}

export interface TeamBoardMember {
  agentId?: string;
  name: string;
  displayBadge?: string;
  role: string;
  isLeader?: boolean;
  tone: number;
  modelId?: string;
  modelLabel?: string;
  modelSwitchable?: boolean;
  modelStatus?: string;
  activeTaskCount?: number;
  unavailableReason?: string;
  models?: RuntimeModelProfile[];
}

interface RuntimeSession {
  session_id?: string;
  live?: string;
  queue_depth?: number;
}

interface RuntimeSnapshot {
  max_active_runs: number;
  global_active: number;
  global_queued: number;
  sessions?: Record<string, RuntimeSession>;
  active_children?: unknown[] | Record<string, unknown[]>;
}

interface TeamBoardSnapshot {
  sessionId: string;
  tasks: Task[];
  runtime: RuntimeSnapshot | null;
  members: TeamBoardMember[];
  teamName: string;
  loaded: boolean;
}

interface NodeLogEntry {
  id: string;
  kind: 'thinking' | 'tool' | 'assistant' | 'status';
  title: string;
  body: string;
  icon?: string;
}

interface SessionFileItem {
  key: string;
  title: string;
  path: string;
  sourceLabel: string;
  summary?: string;
}

const statusLabel: Record<TeamFlowStatus, string> = {
  pending: '待开始',
  running: '进行中',
  blocked: '阻塞',
  completed: '已完成',
  failed: '失败',
  cancelled: '已终止',
};

const statusHint: Record<TeamFlowStatus, string> = {
  pending: '等待 Leader 分配或上游节点完成',
  running: '成员正在处理这个节点',
  blocked: '等待错误、权限、限流或补充信息处理',
  completed: '节点已产出结果，可进入下一步',
  failed: '节点执行失败，建议重试或改派',
  cancelled: '节点已终止',
};

const dagStrategyLabel: Record<string, string> = {
  fast_minimal_path: 'Fast DAG',
  standard_role_dag: 'Standard Role DAG',
  standard_semantic_dag: 'Standard Semantic DAG',
  heavy_single_dag: 'AI Planner DAG',
  heavy_multi_candidate: 'AI Planner DAG',
};

const workflowLaneOrder: Record<string, number> = {
  lead: 10,
  plan: 20,
  design: 30,
  build: 40,
  verify: 50,
  release: 60,
  docs: 70,
  summary: 80,
  other: 90,
};

const workflowLaneTitle: Record<string, string> = {
  lead: 'Leader 拆分任务',
  plan: '任务规划',
  design: '方案设计',
  build: '成员执行任务',
  verify: '测试验证',
  release: '交付整理',
  docs: '文档整理',
  summary: 'Leader 汇总结果',
  other: '协作节点',
};

const planNodeTitle: Record<string, string> = {
  leader_plan: 'Leader 拆分任务',
  leader_review: 'Leader 审阅结果',
  leader_summary: 'Leader 汇总结果',
  fast_execute: '成员执行任务',
  fast_verify: '测试验证',
  build_design: '实现方案',
  test_plan: '测试方案',
  build: '开发实现',
  verify: '测试验证',
  docs: '文档整理',
  release: '交付整理',
};

const snapshots = new Map<string, TeamBoardSnapshot>();
const stableNodes = new Map<string, TeamFlowNode[]>();
const expandedTurns = new Map<string, Set<string>>();
const knownTurns = new Map<string, Set<string>>();
const expandedNodes = new Map<string, Set<string>>();
const openExecutions = new Map<string, Set<string>>();
const filesOpen = new Set<string>();
const stableProgress = new Map<string, { turnId: string; completed: number; total: number; percent: number }>();
const refreshInFlight = new Set<string>();
let refreshTimer: number | null = null;
let pollingSessionId = '';

/** 与 Web TeamAgentTurnBubble 一致：逻辑 leader/member id 解析为团队配置中的真实成员。 */
export function resolveTeamCollaborationMember(
  sessionId: string | null | undefined,
  message: ChatMessage,
): TeamBoardMember | undefined {
  if (!sessionId) return undefined;
  const members = snapshots.get(sessionId)?.members || [];
  return (message.agentId
    ? members.find((member) => member.agentId === message.agentId)
    : undefined)
    || (message.agentName
      ? members.find((member) => member.name === message.agentName)
      : undefined)
    || (message.isLeader ? members.find((member) => member.isLeader) : undefined);
}

export function resolveTeamCollaborationName(
  sessionId: string | null | undefined,
): string | undefined {
  if (!sessionId) return undefined;
  return snapshots.get(sessionId)?.teamName || undefined;
}

function progressText(task: Task, key: string): string {
  return String(task.progress?.[key] || '').trim();
}

function progressNumber(task: Task, key: string): number | null {
  const value = task.progress?.[key];
  const number = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(number) ? number : null;
}

function progressStringList(task: Task, key: string): string[] {
  const value = task.progress?.[key];
  return Array.isArray(value) ? value.map((item) => String(item || '').trim()).filter(Boolean) : [];
}

function progressRecord(task: Task, key: string): Record<string, unknown> {
  const value = task.progress?.[key];
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function stringList(value: unknown): string[] {
  return Array.isArray(value) ? value.map((item) => String(item || '').trim()).filter(Boolean) : [];
}

function plainText(value: unknown): string {
  return String(value || '')
    .split(/\r?\n/)
    .filter((line) => !/^\|.*\|$/.test(line.trim()))
    .join(' ')
    .replace(/```[\s\S]*?```/g, ' ')
    .replace(/`([^`]+)`/g, '$1')
    .replace(/\*\*([^*]+)\*\*/g, '$1')
    .replace(/\*([^*]+)\*/g, '$1')
    .replace(/__([^_]+)__/g, '$1')
    .replace(/!\[([^\]]*)\]\([^)]+\)/g, '$1')
    .replace(/\[([^\]]+)\]\([^)]+\)/g, '$1')
    .replace(/^#{1,6}\s+/gm, '')
    .replace(/^\s*[-*+]\s+/gm, '')
    .replace(/\s+/g, ' ')
    .trim();
}

function compactText(value: unknown, max = 90): string {
  const text = plainText(value);
  if (!text) return '';
  return text.length > max ? `${text.slice(0, Math.max(0, max - 1))}...` : text;
}

function compactSummaryItems(value: unknown, maxItems = 4, maxLength = 120): string[] {
  const lines = String(value || '')
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
    .filter((line) => !/^```/.test(line) && !/^\|.*\|$/.test(line));
  const source = (lines.length ? lines : [String(value || '')])
    .flatMap((line) => plainText(line).split(/[；;]\s*/))
    .map((item) => compactText(item, maxLength))
    .filter(Boolean);
  return source.slice(0, maxItems);
}

function compactSummary(value: unknown, max = 180): string {
  const text = compactSummaryItems(value, 3, max).join('；');
  return text.length > max ? `${text.slice(0, max - 1)}...` : text;
}

export function normalizeTeamFlowStatus(task: Task): TeamFlowStatus {
  const raw = String(task.status || '').toLowerCase();
  if (['done', 'completed', 'success'].includes(raw)) return 'completed';
  if (['running', 'in_progress'].includes(raw)) return 'running';
  if (['pending', 'ready', 'queued'].includes(raw)) return 'pending';
  if (['cancelled', 'canceled'].includes(raw)) return 'cancelled';
  if (['blocked', 'waiting_input', 'waiting'].includes(raw)) return 'blocked';
  if (raw === 'failed' || task.error) return 'failed';
  return 'pending';
}

function ownerOf(task: Task): string {
  const assignee = String(task.assignee || task.progress?.assignee || '').trim();
  if (assignee) return assignee;
  if (task.status === 'blocked' && task.progress?.runtime_blocking) return '待分配';
  if (task.kind === 'team') return 'Team';
  if (task.kind === 'subagent') return 'Subagent';
  if (task.kind === 'agent_turn') return 'Leader';
  return String(task.kind || 'Crew');
}

function cleanAgentName(value: unknown): string {
  const normalized = String(value || '')
    .trim()
    .replace(/^crew::/i, '')
    .replace(/^agent[:：]\s*/i, '')
    .replace(/^member[:：]\s*/i, '')
    .trim();
  if (normalized === 'general-purpose') return 'subagent';
  return normalized.length > 18 ? `${normalized.slice(0, 17)}…` : normalized;
}

function agentsOf(task: Task): string[] {
  const agents: string[] = [];
  const add = (value: unknown): void => {
    const agent = cleanAgentName(value);
    if (agent && !agents.includes(agent)) agents.push(agent);
  };
  const progress = task.progress || {};
  const assignee = cleanAgentName(task.assignee || progress.assignee);
  if (task.kind === 'team' && TEAM_PLAN_SOURCES.has(progressText(task, 'source'))) {
    add(assignee.toLowerCase() === 'leader' ? 'Leader' : assignee);
    return agents;
  }
  if (task.kind === 'team' && assignee && assignee.toLowerCase() !== 'leader') add('Leader');
  if (task.kind === 'agent_turn') add('Leader');
  add(assignee);
  add(progress.agent);
  add(progress.member);
  add(progress.requester_member_id);
  add(progress.sender_member_id);
  if (agents.length === 0) add(ownerOf(task));
  return agents.slice(0, 4);
}

function nodeFullTitle(task: Task): string {
  return String(task.progress?.full_title || task.progress?.plan_node_title || task.progress?.title || task.title || '').trim();
}

function nodeTitle(task: Task, index: number): string {
  if (task.kind === 'team' && TEAM_PLAN_SOURCES.has(progressText(task, 'source'))) {
    const planId = String(task.progress?.plan_node_id || task.task_id || task.id || '').trim();
    const displayTitle = progressText(task, 'display_title');
    if (displayTitle) return compactText(displayTitle, 24);
    const explicitTitle = nodeFullTitle(task);
    const structuralTitle = planId ? planNodeTitle[planId] : '';
    if (explicitTitle) {
      if (structuralTitle && (explicitTitle.startsWith(`${structuralTitle}：`) || explicitTitle.startsWith(`${structuralTitle}:`))) {
        return structuralTitle;
      }
      const firstClause = explicitTitle.split(/[，,。；;：:（）()]/).map((item) => item.trim()).find(Boolean) || explicitTitle;
      return compactText(firstClause, 18);
    }
    if (structuralTitle) return structuralTitle;
    const lane = progressText(task, 'workflow_lane');
    if (workflowLaneTitle[lane]) return workflowLaneTitle[lane];
    const roleLabel = progressText(task, 'role_label');
    return roleLabel ? `${roleLabel}节点` : `协作节点 ${index + 1}`;
  }
  return String(task.title || '').trim() || `${task.kind || '任务'} 节点 ${index + 1}`;
}

function contractSummaryItems(task: Task): string[] {
  const explicit = progressStringList(task, 'summary_items');
  if (explicit.length) return explicit;
  const contract = progressRecord(task, 'result_contract');
  return [
    ['结论', contract.answer],
    ['依据', contract.evidence],
    ['风险', contract.risk],
    ['建议', contract.next_action],
  ].map(([label, value]) => {
    const text = String(value || '').trim();
    if (!text) return '';
    return text.startsWith(`${label}：`) || text.startsWith(`${label}:`) ? text : `${label}：${text}`;
  }).filter(Boolean);
}

function summaryItems(task: Task): string[] {
  const contract = contractSummaryItems(task);
  if (contract.length) return contract;
  if (task.error) {
    const kind = progressText(task, 'error_kind');
    if (kind === 'rate_limit') return ['模型请求触发限流，可稍后重试或降低并发。'];
    if (kind === 'delegate_tool_unavailable') return ['派活工具不可用：请确认当前会话是 Team 模式，并由 Crew 内部 Leader 发起。'];
    return compactSummaryItems(task.error);
  }
  if (task.result) return compactSummaryItems(task.result);
  const progress = task.progress || {};
  const text = String(progress.last_chunk || progress.output_tail || progress.last_tool || '').trim();
  if (text) return compactSummaryItems(text);
  return [statusHint[normalizeTeamFlowStatus(task)]];
}

function taskTime(task: Task): number {
  for (const value of [task.started_at, task.created_at, task.last_activity_at, task.updated_at, task.finished_at]) {
    if (typeof value === 'number' && Number.isFinite(value)) return value;
  }
  return Number.MAX_SAFE_INTEGER;
}

function displayRank(task: Task): number {
  return progressNumber(task, 'display_order') ?? workflowLaneOrder[progressText(task, 'workflow_lane')] ?? workflowLaneOrder.other;
}

function sortTasks(tasks: Task[]): Task[] {
  return [...tasks].sort((a, b) => taskTime(a) - taskTime(b)
    || displayRank(a) - displayRank(b)
    || String(a.id || a.task_id || '').localeCompare(String(b.id || b.task_id || '')));
}

export function makeTeamFlowNodes(tasks: Task[]): TeamFlowNode[] {
  return sortTasks(tasks).map((task, index) => {
    const items = summaryItems(task);
    return {
      id: task.task_id || task.id || `node_${index}`,
      title: nodeTitle(task, index),
      fullTitle: nodeFullTitle(task),
      owner: ownerOf(task),
      agents: agentsOf(task),
      status: normalizeTeamFlowStatus(task),
      summary: compactSummary(items.join('；')) || statusHint[normalizeTeamFlowStatus(task)],
      summaryItems: items,
      raw: task,
    };
  });
}

function planNodeId(node: TeamFlowNode): string {
  return String(node.raw.progress?.plan_node_id || node.id || '').trim();
}

function parentNodeIds(node: TeamFlowNode): string[] {
  return stringList(node.raw.progress?.parent_node_ids);
}

function stageStatus(stage: TeamFlowStage): TeamFlowStatus {
  if (stage.nodes.some((node) => node.status === 'running')) return 'running';
  if (stage.nodes.some((node) => node.status === 'failed')) return 'failed';
  if (stage.nodes.some((node) => node.status === 'blocked')) return 'blocked';
  if (stage.nodes.every((node) => node.status === 'completed')) return 'completed';
  if (stage.nodes.every((node) => node.status === 'cancelled')) return 'cancelled';
  return 'pending';
}

function makeStages(nodes: TeamFlowNode[]): TeamFlowStage[] {
  const hasPlanNodes = nodes.some((node) => TEAM_PLAN_SOURCES.has(progressText(node.raw, 'source')));
  if (!hasPlanNodes) return nodes.map((node, index) => ({ id: `stage_${index}`, depth: index, nodes: [node] }));
  const byId = new Map<string, TeamFlowNode>();
  for (const node of nodes) {
    for (const id of [planNodeId(node), node.raw.task_id, node.raw.id]) {
      const value = String(id || '').trim();
      if (value) byId.set(value, node);
    }
  }
  const memo = new Map<string, number>();
  const visiting = new Set<string>();
  const depthOf = (node: TeamFlowNode): number => {
    const id = planNodeId(node);
    if (!id) return 0;
    const cached = memo.get(id);
    if (cached !== undefined) return cached;
    if (visiting.has(id)) return 0;
    visiting.add(id);
    const parents = parentNodeIds(node).map((parent) => byId.get(parent)).filter((item): item is TeamFlowNode => Boolean(item));
    const depth = parents.length ? Math.max(...parents.map(depthOf)) + 1 : 0;
    visiting.delete(id);
    memo.set(id, depth);
    return depth;
  };
  const groups = new Map<number, TeamFlowNode[]>();
  for (const node of nodes) {
    const depth = depthOf(node);
    groups.set(depth, [...(groups.get(depth) || []), node]);
  }
  return [...groups.entries()]
    .sort(([a], [b]) => a - b)
    .map(([depth, stageNodes]) => ({
      id: `stage_${depth}`,
      depth,
      nodes: stageNodes.sort((a, b) => displayRank(a.raw) - displayRank(b.raw) || a.title.localeCompare(b.title)),
    }));
}

export function makeTeamFlowTurns(nodes: TeamFlowNode[]): TeamFlowTurn[] {
  const groups = new Map<string, TeamFlowNode[]>();
  for (const node of nodes) {
    const id = String(node.raw.progress?.turn_session_id || node.raw.session_id || 'team_turn').trim();
    groups.set(id, [...(groups.get(id) || []), node]);
  }
  return [...groups.entries()].map(([id, groupNodes], index) => {
    const stages = makeStages(groupNodes);
    const first = groupNodes[0];
    const rawTitle = String(first?.raw.progress?.turn_title || first?.raw.detail || first?.title || '').trim();
    return {
      id,
      title: rawTitle ? compactText(rawTitle, 42) : `团队任务 ${index + 1}`,
      stages,
      status: stageStatus({ id: `${id}:all`, depth: -1, nodes: groupNodes }),
    };
  });
}

function currentTurnNodes(nodes: TeamFlowNode[], turns: TeamFlowTurn[]): TeamFlowNode[] {
  const turn = turns[turns.length - 1];
  if (!turn) return nodes;
  const seen = new Set<string>();
  return turn.stages.flatMap((stage) => stage.nodes).filter((node) => {
    if (seen.has(node.id)) return false;
    seen.add(node.id);
    return true;
  });
}

function dependencyLabel(node: TeamFlowNode, nodes: TeamFlowNode[]): string {
  const parents = parentNodeIds(node);
  if (!parents.length) return '';
  const byId = new Map<string, string>();
  for (const item of nodes) {
    for (const id of [planNodeId(item), item.raw.task_id, item.raw.id]) {
      const value = String(id || '').trim();
      if (value) byId.set(value, item.title);
    }
  }
  return parents.map((id) => compactText(byId.get(id) || id, 18)).join('、');
}

function artifactPaths(task: Task): string[] {
  const fromProgress = stringList(task.progress?.artifact_paths);
  const fromOutput = String(task.output_ref || '')
    .split(/\r?\n/)
    .map((item) => item.trim())
    .filter(Boolean)
    .filter((path) => !/(?:^|[\\/])tasks[\\/][^\\/]+\.json$/i.test(path));
  return Array.from(new Set([...fromProgress, ...fromOutput]));
}

function fileNameOf(filePath: string): string {
  return filePath.split(/[\\/]/).filter(Boolean).pop() || filePath || '产物';
}

function collectSessionFiles(tasks: Task[], messages: ChatMessage[]): SessionFileItem[] {
  const items: SessionFileItem[] = [];
  const seen = new Set<string>();
  const push = (filePath: string, sourceLabel: string, summary = ''): void => {
    const path = String(filePath || '').trim();
    const key = path.toLowerCase();
    if (!path || seen.has(key)) return;
    seen.add(key);
    items.push({ key, path, title: fileNameOf(path), sourceLabel, ...(summary ? { summary } : {}) });
  };
  for (const task of tasks) {
    const label = progressText(task, 'title') || progressText(task, 'plan_node_title') || compactText(task.title || task.id, 28);
    for (const path of artifactPaths(task)) push(path, label || '节点产物');
  }
  for (const message of messages) {
    for (const file of message.turnFileChanges || []) push(file.path, message.agentName || 'Team 消息', file.status);
  }
  return items;
}

function durationLabel(task: Task): string {
  const start = typeof task.started_at === 'number' ? task.started_at : typeof task.created_at === 'number' ? task.created_at : 0;
  const end = typeof task.finished_at === 'number' ? task.finished_at : typeof task.updated_at === 'number' ? task.updated_at : 0;
  if (!start || !end || end < start) return '';
  const seconds = Math.max(0, Math.round(end - start));
  if (seconds < 1) return '<1s';
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  return seconds % 60 ? `${minutes}m ${seconds % 60}s` : `${minutes}m`;
}

function logText(value: unknown): string {
  const text = String(value || '').replace(/\r\n/g, '\n').replace(/\r/g, '\n').replace(/\n{4,}/g, '\n\n\n').trim();
  return text.length > 1_600 ? `${text.slice(0, 1_600)}\n...` : text;
}

function nodeLogs(node: TeamFlowNode): NodeLogEntry[] {
  const entries: NodeLogEntry[] = [];
  const seen = new Set<string>();
  const add = (entry: NodeLogEntry): void => {
    const key = `${entry.kind}:${entry.title}:${entry.body.slice(0, 180)}`;
    if (!seen.has(key)) {
      seen.add(key);
      entries.push(entry);
    }
  };
  const events = Array.isArray(node.raw.progress?.execution_events)
    ? node.raw.progress.execution_events as Record<string, unknown>[]
    : [];
  for (const event of events.slice(-6)) {
    const rawKind = String(event.kind || event.event_type || 'status');
    const title = String(event.event_title || event.title || '执行事件');
    const body = logText(event.event_text || event.body || event.message);
    if (title === '节点承接') continue;
    const kind: NodeLogEntry['kind'] = rawKind === 'tool' || rawKind === 'thinking' || rawKind === 'assistant' ? rawKind : 'status';
    add({ id: String(event.id || `${node.id}_event_${entries.length}`), kind, title, body, icon: String(event.event_icon || rawKind) });
  }
  if (!entries.length && node.raw.error) add({ id: `${node.id}_error`, kind: 'tool', title: '错误日志', body: logText(node.raw.error), icon: 'tool' });
  return entries.slice(-8);
}

function logIcon(entry: NodeLogEntry): string {
  const icon = String(entry.icon || entry.kind).toLowerCase();
  if (entry.kind === 'thinking' || icon.includes('think')) return '思';
  if (entry.kind === 'tool' || icon.includes('tool')) return '工';
  if (entry.kind === 'assistant' || icon.includes('assistant')) return '答';
  if (icon.includes('route') || icon.includes('replan')) return '路';
  if (icon.includes('alert')) return '!';
  if (icon.includes('spark') || icon.includes('reflection')) return '省';
  return '态';
}

function compactRolePhrase(value: string, max = 10): string {
  const text = value
    .replace(/^\s*\d+[.、]\s*/, '')
    .replace(/^\s*[-*]\s*/, '')
    .replace(/^(作为)?(团队)?(leader|Leader|负责人|角色)[:：]?/i, '')
    .replace(/^根据.*?(进行|完成|开展)/, '')
    .replace(/^(主要)?(负责|承担|进行|参与|协助|执行)/, '')
    .replace(/^(整体|最终|相关|对应)的?/, '')
    .replace(/的(?=编码|开发|实现|测试|验收|评审|审查|设计|规划|管理)/g, '')
    .replace(/\s+/g, '')
    .trim();
  if (!text) return '';
  return text.length <= max ? text : `${text.slice(0, max)}…`;
}

function responsibilityBlock(role: string): string {
  const text = String(role || '');
  const match = text.match(/(?:^|\n)##\s*职责\s*\n([\s\S]*?)(?=\n##\s|\n#\s|$)/);
  return match?.[1] || text;
}

function splitRoleClauses(role: string, max = 12): string[] {
  return responsibilityBlock(role)
    .replace(/[`*_#>]/g, '')
    .split(/\n/)
    .filter((line) => !/^\s*(工作原则|职责|团队协作关系|输出格式|工作安排|备注)[:：]?\s*$/.test(line))
    .join('\n')
    .split(/[；;。.!！\n，,、/|｜+&和及]/)
    .map((part) => compactRolePhrase(part, max))
    .filter((part) => part && !/工作原则|团队协作关系|输出格式|工作安排|每日|提交前|迭代开始|迭代中|迭代结束/.test(part));
}

function summarizeLeaderCoord(role: string): string {
  const text = responsibilityBlock(role).replace(/\s+/g, '');
  const parts: string[] = [];
  if (/拆解|分配|派活|规划/.test(text)) parts.push('任务拆解');
  if (/协调|协同|沟通|推进|跟踪/.test(text)) parts.push('协作推进');
  if (/汇总|反馈|总结|交付/.test(text)) parts.push('结果汇总');
  return parts.length ? `Leader 统筹${parts.slice(0, 2).join('、')}` : 'Leader 统筹团队协作';
}

function summarizeBusinessDuty(role: string, fallback: string, options: { leader?: boolean; max?: number } = {}): string {
  const max = options.max ?? 10;
  const clauses = splitRoleClauses(role, max)
    .filter((part) => !/leader|Leader|统筹|拆解|分配|派活|协调|协同|沟通|推进|跟踪|汇总|反馈|总结/.test(part));
  const priority = options.leader
    ? [
        /测试|验收|质检|质量|回归/,
        /评审|审查|把关/,
        /计划|里程碑|项目/,
        /开发|编码|实现|工程|前端|后端|接口/,
        /架构|设计/,
        /研究|调研|方案|文档|记录/,
      ]
    : [
        /开发|编码|实现|工程|前端|后端|接口/,
        /测试|验收|质检|质量|回归/,
        /架构|设计/,
        /评审|审查|把关/,
        /研究|调研|方案|文档|记录/,
      ];
  const source = priority.map((pattern) => clauses.find((part) => pattern.test(part))).find(Boolean)
    || clauses[0]
    || splitRoleClauses(role, max)[0]
    || '';
  return source ? compactRolePhrase(source, max) || fallback : fallback;
}

function summarizeLeaderRole(role: string): string {
  const coord = summarizeLeaderCoord(role);
  const duty = summarizeBusinessDuty(role, '', { leader: true, max: 80 });
  return duty ? `${coord}，负责${duty}` : coord;
}

function membersFromTeam(
  team: ExternalTeam | undefined,
  agents: ExternalAgent[],
  modelBindings: TeamMemberModelBinding[] = [],
): TeamBoardMember[] {
  if (!team) return [];
  const agentNames = new Map(agents.map((agent) => [agent.id, agent.name]));
  const modelByMemberId = new Map(modelBindings.map((binding) => [binding.member_id, binding]));
  const rows = [...(team.members || [])].sort((a, b) => (a.sort_order ?? 0) - (b.sort_order ?? 0));
  const leaderId = String(team.leader_agent_id || '').trim();
  const ordered = [
    ...rows.filter((row) => row.agent_id === leaderId),
    ...rows.filter((row) => row.agent_id !== leaderId),
  ];
  return ordered.map((member, index) => {
    const isLeader = member.agent_id === leaderId;
    const isBuiltin = member.agent_id === CREW_BUILTIN_AGENT_ID;
    const model = modelByMemberId.get(member.agent_id);
    return {
      agentId: member.agent_id,
      name: isBuiltin ? 'Crew' : member.agent_name || agentNames.get(member.agent_id) || member.agent_id,
      displayBadge: member.display_badge || (isBuiltin ? 'M' : '?'),
      role: isLeader
        ? summarizeLeaderRole(member.role || '')
        : `负责${summarizeBusinessDuty(member.role || '', '协作执行', { max: 80 })}`,
      isLeader,
      tone: index % 6,
      ...(model?.model_profile_id ? { modelId: model.model_profile_id } : {}),
      ...(model && (model.model_label || model.model_profile_id)
        ? { modelLabel: model.model_label || model.model_profile_id || '未配置模型' }
        : {}),
      ...(model?.model_switchable !== undefined ? { modelSwitchable: model.model_switchable } : {}),
      ...(model?.status ? { modelStatus: model.status } : {}),
      ...(model?.active_task_count !== undefined ? { activeTaskCount: model.active_task_count } : {}),
      ...(model?.unavailable_reason ? { unavailableReason: model.unavailable_reason } : {}),
      ...(model?.models?.length ? { models: model.models } : {}),
    };
  });
}

function inferredMembers(nodes: TeamFlowNode[]): TeamBoardMember[] {
  const names: string[] = [];
  const roles = new Map<string, string>();
  for (const node of nodes) {
    for (const agent of node.agents) {
      if (!names.includes(agent)) names.push(agent);
      const role = progressText(node.raw, 'role_label');
      if (role && !roles.has(agent)) roles.set(agent, role);
    }
  }
  if (!names.length) names.push('Leader');
  if (!names.includes('Leader')) names.unshift('Leader');
  return names.slice(0, 6).map((name, index) => ({
    ...(name === 'Leader' ? { agentId: CREW_BUILTIN_AGENT_ID } : {}),
    name: name === 'Leader' ? 'Crew' : name,
    role: roles.get(name) || '按节点协议处理子任务',
    isLeader: name === 'Leader',
    tone: index % 6,
  }));
}

async function loadConfiguredTeam(sessionId: string): Promise<{ name: string; members: TeamBoardMember[] }> {
  const config = await backendApi.getSessionAgentConfig(sessionId).catch(() => null);
  const teamId = String(config?.team?.external_team_id || state.activeExternalTeamIdBySession[sessionId] || '').trim();
  if (!teamId) return { name: '', members: [] };
  const [teams, agents, modelBinding] = await Promise.all([
    backendApi.externalTeams().catch(() => []),
    backendApi.externalAgents().catch(() => []),
    backendApi.getSessionModel(sessionId).catch(() => null),
  ]);
  const team = teams.find((item) => item.id === teamId);
  return {
    name: String(team?.name || '').trim(),
    members: membersFromTeam(team, agents, modelBinding?.members || []),
  };
}

/** 首次发送 Team 消息前预热真实成员身份，避免逻辑 `leader` 短暂渲染为 L。 */
export async function primeTeamCollaborationIdentity(sessionId: string): Promise<void> {
  const previous = snapshots.get(sessionId);
  if (previous?.members.length && previous.teamName) return;
  const configured = await loadConfiguredTeam(sessionId);
  if (!configured.members.length && !configured.name) return;
  snapshots.set(sessionId, {
    sessionId,
    tasks: previous?.tasks || [],
    runtime: previous?.runtime || null,
    members: configured.members.length ? configured.members : previous?.members || [],
    teamName: configured.name || previous?.teamName || '',
    loaded: previous?.loaded || false,
  });
}

export async function refreshTeamCollaborationBoard(sessionId: string | null | undefined = state.activeSessionId): Promise<void> {
  if (!sessionId || refreshInFlight.has(sessionId)) return;
  refreshInFlight.add(sessionId);
  try {
    const previous = snapshots.get(sessionId);
    const [tasks, runtime, configured] = await Promise.all([
      backendApi.tasks(sessionId).catch(() => previous?.tasks || []),
      backendApi.runtimeConcurrency().catch(() => previous?.runtime || null),
      previous?.members.length && previous.teamName
        ? Promise.resolve({ name: previous.teamName, members: previous.members })
        : loadConfiguredTeam(sessionId),
    ]);
    const next: TeamBoardSnapshot = {
      sessionId,
      tasks,
      runtime: runtime as RuntimeSnapshot | null,
      members: configured.members,
      teamName: configured.name,
      loaded: true,
    };
    const changed = !previous || JSON.stringify(previous) !== JSON.stringify(next);
    snapshots.set(sessionId, next);
    if (tasks.length) stableNodes.set(sessionId, makeTeamFlowNodes(tasks));
    // 轮询结果未变化时不销毁看板 DOM，保证滚动条拖拽不中断、不回弹。
    if (changed) {
      window.dispatchEvent(new CustomEvent('team-collaboration:updated', { detail: { sessionId } }));
    }
  } finally {
    refreshInFlight.delete(sessionId);
  }
}

export function startTeamCollaborationPolling(sessionId: string): void {
  if (pollingSessionId === sessionId && refreshTimer !== null) return;
  stopTeamCollaborationPolling();
  pollingSessionId = sessionId;
  void refreshTeamCollaborationBoard(sessionId);
  refreshTimer = window.setInterval(() => {
    if (state.activeSessionId === sessionId) void refreshTeamCollaborationBoard(sessionId);
  }, REFRESH_MS);
}

export function stopTeamCollaborationPolling(): void {
  if (refreshTimer !== null) window.clearInterval(refreshTimer);
  refreshTimer = null;
  pollingSessionId = '';
}

/** 清理模块级 UI 快照；仅供单元测试隔离用。 */
export function __resetTeamCollaborationBoardForTest(): void {
  stopTeamCollaborationPolling();
  snapshots.clear();
  stableNodes.clear();
  expandedTurns.clear();
  knownTurns.clear();
  expandedNodes.clear();
  openExecutions.clear();
  filesOpen.clear();
  stableProgress.clear();
  refreshInFlight.clear();
}

export function teamCollaborationTaskCount(sessionId: string | null | undefined = state.activeSessionId): number {
  return sessionId ? snapshots.get(sessionId)?.tasks.length || 0 : 0;
}

function htmlAttr(value: unknown): string {
  return escapeHtml(String(value || ''));
}

function teamMarkHtml(): string {
  return '<span class="team-mark team-mark--hero" aria-hidden="true"><i></i><i></i></span>';
}

function crewAvatarHtml(): string {
  return '<span class="team-member__crew-avatar" aria-hidden="true"><svg class="msg__avatar-symbol" viewBox="0 0 32 32"><use href="#avatar-headphones"></use></svg></span>';
}

function folderIconHtml(): string {
  return '<svg class="mw-icon" viewBox="0 0 24 24" aria-hidden="true"><use href="#icon-folder"></use></svg>';
}

function memberModelHtml(member: TeamBoardMember): string {
  const models = member.models || [];
  if (!models.length) return '';
  const busy = member.modelStatus === 'running' || (member.activeTaskCount || 0) > 0;
  const disabled = member.modelSwitchable === false || busy;
  const reason = member.unavailableReason || (busy ? '成员运行中，结束后可切换' : '当前成员运行时不支持模型切换');
  const options = models.map((model) => `<option value="${htmlAttr(model.id)}"${model.id === member.modelId ? ' selected' : ''}>${escapeHtml(model.label || model.id)}</option>`).join('');
  return `<label class="team-member__model" title="${htmlAttr(disabled ? reason : '切换该成员模型')}"><span>模型</span><select class="team-member__model-select" data-team-member-model data-team-member-id="${htmlAttr(member.agentId || '')}" aria-label="${htmlAttr(`${member.name}模型`)}"${disabled ? ' disabled' : ''}>${options}</select></label>`;
}

function renderMembers(members: TeamBoardMember[]): string {
  return members.map((member) => `
    <div class="team-member${member.isLeader ? ' is-leader' : ''}">
      ${member.agentId === CREW_BUILTIN_AGENT_ID
        ? crewAvatarHtml()
        : `<span class="agent-avatar agent-tone-${member.tone}">${escapeHtml(member.displayBadge || '?')}</span>`}
      <div class="team-member__copy"><strong>${escapeHtml(member.name)}${member.isLeader ? '<span class="pixel-flag" aria-label="Leader" title="Leader"></span>' : ''}</strong><p>${escapeHtml(member.role)}</p>${memberModelHtml(member)}</div>
    </div>
  `).join('');
}

function renderFiles(sessionId: string, files: SessionFileItem[]): string {
  if (!filesOpen.has(sessionId)) return '';
  return `<section class="board-files-panel" aria-label="当前 session 文件清单">
    <div class="board-files-panel__head"><strong>产物文件</strong><span>${files.length ? `${files.length} 个` : '暂无'}</span></div>
    ${files.length
      ? `<div class="board-files-panel__list">${files.map((file) => `<button type="button" class="board-file-item" data-team-open-path="${htmlAttr(file.path)}" title="${htmlAttr(file.path)}"><i class="flow-node__file-icon" aria-hidden="true"></i><span><strong>${escapeHtml(file.title)}</strong><em>${escapeHtml(file.sourceLabel)}${file.summary ? ` · ${escapeHtml(file.summary)}` : ''}</em></span></button>`).join('')}</div>`
      : '<p class="board-files-panel__empty">团队产出文件后，会在这里汇总成当前 session 的文件清单。</p>'}
  </section>`;
}

function renderNode(
  sessionId: string,
  node: TeamFlowNode,
  nodes: TeamFlowNode[],
  messages: ChatMessage[],
  leaderName: string,
  members: TeamBoardMember[],
): string {
  const isExpanded = expandedNodes.get(sessionId)?.has(node.id) || false;
  const dependency = dependencyLabel(node, nodes);
  const displayAgent = node.owner.toLowerCase() === 'leader' ? leaderName : node.owner;
  const previousAssignee = String(node.raw.progress?.previous_assignee || '').trim();
  const assignmentDetail = node.owner === '待分配' && previousAssignee
    ? `<span>原主责 ${escapeHtml(previousAssignee)}</span>`
    : '';
  if (!isExpanded) {
    return `<article class="flow-node is-${node.status}"><div class="flow-node__card"><button class="flow-node__summary-btn" type="button" data-team-node="${htmlAttr(node.id)}" aria-expanded="false"><div class="flow-node__top"><span class="agent-chip">主责：${escapeHtml(displayAgent)}</span><span class="flow-status is-${node.status}">${statusLabel[node.status]}</span></div><strong class="flow-node__title" title="${htmlAttr(node.fullTitle || node.title)}">${escapeHtml(node.title)}</strong>${dependency ? `<span class="flow-node__dependency">依赖：${escapeHtml(dependency)}</span>` : ''}<span class="flow-node__hint">点击展开详情</span></button></div></article>`;
  }
  const logs = nodeLogs(node);
  const paths = artifactPaths(node.raw);
  const duration = durationLabel(node.raw);
  const toolCount = logs.filter((entry) => entry.kind === 'tool').length;
  const executionOpen = openExecutions.get(sessionId)?.has(node.id) || false;
  const summaryItems = node.summaryItems.length ? node.summaryItems : [node.summary];
  const recoveryMembers = members.filter((member) => !member.isLeader && member.name);
  const recoveryActions = node.status === 'blocked'
    ? `<div class="flow-node__actions flow-node__recovery-actions"><span>阻塞节点处理</span>${previousAssignee ? `<button class="mini-cancel" type="button" data-team-recover-action="retry" data-team-recover-node="${htmlAttr(node.id)}" data-team-recover-assignee="${htmlAttr(previousAssignee)}">重试原成员</button>` : ''}${recoveryMembers.length ? `<select class="mini-recovery-select" data-team-recovery-assignee="${htmlAttr(node.id)}" aria-label="选择恢复成员">${recoveryMembers.map((member) => `<option value="${htmlAttr(member.name)}">${escapeHtml(member.name)}</option>`).join('')}</select><button class="mini-cancel" type="button" data-team-recover-action="reassign" data-team-recover-node="${htmlAttr(node.id)}">重新分配</button>` : ''}<button class="mini-cancel" type="button" data-team-recover-action="abandon" data-team-recover-node="${htmlAttr(node.id)}">放弃节点</button></div>`
    : '';
  return `<article class="flow-node is-${node.status}"><div class="flow-node__card">
    <button class="flow-node__summary-btn" type="button" data-team-node="${htmlAttr(node.id)}" aria-expanded="true"><div class="flow-node__top"><span class="agent-chip">主责：${escapeHtml(displayAgent)}</span><span class="flow-status is-${node.status}">${statusLabel[node.status]}</span></div><strong class="flow-node__title" title="${htmlAttr(node.fullTitle || node.title)}">${escapeHtml(node.title)}</strong>${dependency ? `<span class="flow-node__dependency">依赖：${escapeHtml(dependency)}</span>` : ''}<span class="flow-node__hint">收起详情</span></button>
    <div class="flow-node__detail">
      <section class="flow-node__brief" aria-label="节点摘要"><div class="flow-node__meta-line"><span>负责人 ${escapeHtml(displayAgent)}</span>${assignmentDetail}<span>${statusLabel[node.status]}</span>${duration ? `<span>${duration}</span>` : ''}</div>${node.fullTitle && node.fullTitle !== node.title ? `<p class="flow-node__full-title">节点任务：${escapeHtml(node.fullTitle)}</p>` : ''}<ul class="flow-node__brief-list">${summaryItems.map((item) => `<li>${escapeHtml(item)}</li>`).join('')}</ul></section>
      ${paths.length ? `<section class="flow-node__artifacts"><span>产物</span>${paths.map((path) => `<button type="button" data-team-open-path="${htmlAttr(path)}" title="${htmlAttr(path)}"><i class="flow-node__file-icon" aria-hidden="true"></i><strong>${escapeHtml(fileNameOf(path))}</strong></button>`).join('')}</section>` : ''}
      <details class="flow-node__execution" data-team-execution="${htmlAttr(node.id)}"${executionOpen ? ' open' : ''}><summary><span>执行详情</span><em>${logs.length} 条事件${toolCount ? ` · ${toolCount} 个工具` : ''}</em></summary>${logs.length ? `<div class="flow-node__timeline">${logs.map((entry) => `<article class="flow-log is-${entry.kind}"><span class="flow-log__icon" aria-hidden="true">${logIcon(entry)}</span><div class="flow-log__body"><strong>${escapeHtml(entry.title)}</strong><pre>${escapeHtml(entry.body)}</pre></div></article>`).join('')}</div>` : '<p class="flow-node__log-empty">暂无执行日志</p>'}</details>
      ${recoveryActions}${['pending', 'running'].includes(node.raw.status) ? `<div class="flow-node__actions"><button class="mini-cancel" type="button" data-team-cancel-task="${htmlAttr(node.raw.task_id || node.raw.id)}">取消节点</button></div>` : ''}
    </div>
  </div></article>`;
}

function renderRuntime(sessionId: string, runtime: RuntimeSnapshot | null, dagInfo: { label: string; detail: string } | null): string {
  const sessions = Object.entries(runtime?.sessions || {})
    .map(([key, value]) => ({ ...value, session_id: value.session_id || key }))
    .filter((item) => item.live !== 'idle' || (item.queue_depth || 0) > 0);
  const children = runtime?.active_children;
  const activeChildCount = Array.isArray(children)
    ? children.length
    : children && typeof children === 'object'
      ? Object.values(children).reduce((count, items) => count + (Array.isArray(items) ? items.length : 0), 0)
      : 0;
  return `<section class="runtime-box"><div class="runtime-box__head"><span>运行态</span><span>${runtime ? `${runtime.global_active}/${runtime.max_active_runs || '∞'}` : '—'}</span></div><div class="runtime-box__current"><span title="${htmlAttr(sessionId)}">${escapeHtml(sessionId)}</span><em title="${htmlAttr(dagInfo?.detail || '等待 DAG')}">${dagInfo ? `${escapeHtml(dagInfo.label)} · ${escapeHtml(dagInfo.detail)}` : '等待 DAG'}</em></div><div class="runtime-box__grid"><div><span>全局排队</span><b>${runtime?.global_queued ?? 0}</b></div><div><span>活跃子任务</span><b>${activeChildCount}</b></div></div>${sessions.length ? `<div class="runtime-box__sessions">${sessions.slice(0, 4).map((item) => `<div class="runtime-session"><span title="${htmlAttr(item.session_id)}">${escapeHtml(item.session_id || '')}</span><em>${escapeHtml(item.live || 'idle')}${item.queue_depth ? ` · q${item.queue_depth}` : ''}</em></div>`).join('')}</div>` : '<div class="runtime-box__empty">暂无运行或排队会话</div>'}</section>`;
}

export function buildTeamCollaborationBoardHtml(sessionId: string | null | undefined = state.activeSessionId): string {
  if (!sessionId) return '<div class="inspector-empty">请选择一个 Team Session。</div>';
  const snapshot = snapshots.get(sessionId) || {
    sessionId,
    tasks: [],
    runtime: null,
    members: [],
    teamName: '',
    loaded: false,
  };
  const freshNodes = makeTeamFlowNodes(snapshot.tasks);
  if (freshNodes.length) stableNodes.set(sessionId, freshNodes);
  const nodes = freshNodes.length ? freshNodes : stableNodes.get(sessionId) || [];
  const turns = makeTeamFlowTurns(nodes);
  const currentNodes = currentTurnNodes(nodes, turns);
  const currentTurn = turns[turns.length - 1];
  const currentTurnId = currentTurn?.id || '';
  const currentIds = new Set(turns.map((turn) => turn.id));
  const known = knownTurns.get(sessionId) || new Set<string>();
  const expanded = expandedTurns.get(sessionId) || new Set<string>();
  for (const id of currentIds) if (!known.has(id)) expanded.add(id);
  for (const id of [...expanded]) if (!currentIds.has(id)) expanded.delete(id);
  knownTurns.set(sessionId, currentIds);
  expandedTurns.set(sessionId, expanded);

  const completed = currentNodes.filter((node) => node.status === 'completed').length;
  const runningNode = currentNodes.find((node) => node.status === 'running');
  const rawProgress = currentNodes.length ? Math.min(100, Math.round(((completed + (runningNode ? 0.35 : 0)) / currentNodes.length) * 100)) : 0;
  const previousProgress = stableProgress.get(sessionId);
  const progress = previousProgress?.turnId === currentTurnId
    ? {
        turnId: currentTurnId,
        completed: Math.max(previousProgress.completed, completed),
        total: Math.max(previousProgress.total, currentNodes.length),
        percent: Math.max(previousProgress.percent, rawProgress),
      }
    : { turnId: currentTurnId, completed, total: currentNodes.length, percent: rawProgress };
  if (currentNodes.length || !previousProgress) stableProgress.set(sessionId, progress);

  const members = snapshot.members.length ? snapshot.members : inferredMembers(nodes);
  const leaderName = members.find((member) => member.isLeader)?.name || 'Crew';
  const nextNode = currentNodes.find((node) => node.status === 'running')
    || currentNodes.find((node) => node.status === 'pending')
    || currentNodes[currentNodes.length - 1];
  const allCompleted = currentNodes.length > 0 && currentNodes.every((node) => node.status === 'completed');
  const errors = currentNodes.filter((node) => String(node.raw.error || '').trim());
  const files = collectSessionFiles(nodes.map((node) => node.raw), state.messages[sessionId] || []);
  const strategies = currentNodes.map((node) => progressText(node.raw, 'plan_strategy')).filter(Boolean);
  const strategy = strategies[0] || '';
  const tier = currentNodes.map((node) => progressText(node.raw, 'execution_tier')).find(Boolean)
    || (strategy.startsWith('fast_') ? 'fast' : strategy.startsWith('heavy_') ? 'ai' : strategy.startsWith('standard_') ? 'standard' : '');
  const dagInfo = currentNodes.length ? {
    label: dagStrategyLabel[strategy] || (tier ? `${tier[0]?.toUpperCase()}${tier.slice(1)} DAG` : 'Team DAG'),
    detail: [strategy || tier, currentTurn ? `${currentTurn.stages.length}层` : '', `${currentNodes.length}节点`].filter(Boolean).join(' · '),
  } : null;

  const flowHtml = nodes.length ? `<section class="flow-map" aria-label="团队运行流程">${turns.map((turn, turnIndex) => `<section class="flow-turn is-${turn.status}"><button class="flow-turn__head" type="button" data-team-turn="${htmlAttr(turn.id)}" aria-expanded="${expanded.has(turn.id)}"><span>${turnIndex + 1}</span><div><strong>${escapeHtml(turn.title)}</strong><em>${statusLabel[turn.status]}</em></div></button>${expanded.has(turn.id) ? turn.stages.map((stage) => `<div class="flow-stage is-${stageStatus(stage)}"><div class="flow-stage__rail"><span>${stage.depth + 1}</span><i></i></div><div class="flow-stage__nodes">${stage.nodes.map((node) => renderNode(sessionId, node, nodes, state.messages[sessionId] || [], leaderName, members)).join('')}</div></div>`).join('') : ''}</section>`).join('')}</section>` : `<div class="board__empty board__empty--cute"><span class="pixel-empty" aria-hidden="true"></span><strong>${snapshot.loaded ? '还没有流程节点' : '正在加载协作流程'}</strong><p>${snapshot.loaded ? '点击团队派活或开始 Team 会话后，这里会按节点展示负责人、进度和小结。' : '正在读取团队成员、任务节点和运行状态。'}</p></div>`;

  return `<div class="inspector-team-collaboration"><aside class="team-collaboration-board">
    <div class="board__head"><div><span class="board__title-row">协作看板<button class="board-files-btn${filesOpen.has(sessionId) ? ' is-active' : ''}" type="button" data-team-files aria-expanded="${filesOpen.has(sessionId)}" title="查看当前 session 文件清单">${folderIconHtml()}${files.length ? `<i>${files.length}</i>` : ''}</button></span><em>Team Flow</em></div><div class="board__actions"><button class="team-board-close" type="button" data-team-close title="收起">×</button></div></div>
    ${renderFiles(sessionId, files)}
    <div class="board__list team-board__list">
      <section class="flow-hero"><div class="flow-hero__title">${teamMarkHtml()}<div><strong>${nodes.length ? '团队工作流' : '等待团队工作流'}</strong><p>${nextNode ? (allCompleted ? `已完成：${escapeHtml(nextNode.title)}` : `当前节点：${escapeHtml(nextNode.title)}`) : '开始任务后这里会展示流程阶段、负责人和节点小结。'}</p></div></div><div class="flow-meter" aria-label="完成进度 ${progress.percent}%"><span data-team-progress="${progress.percent}"></span></div><div class="flow-hero__meta"><span>${progress.completed}/${progress.total || 0} 已完成</span><span>${runningNode ? `${escapeHtml(runningNode.owner.toLowerCase() === 'leader' ? leaderName : runningNode.owner)} 处理中` : '暂无运行节点'}</span></div><div class="team-members" aria-label="团队成员">${renderMembers(members)}</div></section>
      ${flowHtml}
      ${errors.length ? `<section class="board-alert"><strong>错误待处理</strong><p>${escapeHtml(errors[0]?.title || '')}：${escapeHtml(errors[0]?.summary || '')}</p></section>` : ''}
      ${renderRuntime(sessionId, snapshot.runtime, dagInfo)}
    </div>
  </aside></div>`;
}

async function openPath(filePath: string): Promise<void> {
  if (!window.Crew?.openPath) {
    notify('当前环境不支持打开文件');
    return;
  }
  try {
    const result = await window.Crew.openPath(filePath);
    if (result) notify(`打开失败：${result}`);
  } catch (error) {
    notify(`打开失败：${error instanceof Error ? error.message : String(error)}`);
  }
}

export function activateTeamCollaborationBoard(sessionId: string | null | undefined = state.activeSessionId): void {
  if (!sessionId) return;
  const progressFill = document.querySelector<HTMLElement>('[data-team-progress]');
  if (progressFill) setRuntimeStyle(progressFill, 'width', `${progressFill.dataset.teamProgress ?? '0'}%`);
  startTeamCollaborationPolling(sessionId);
  document.querySelectorAll<HTMLButtonElement>('[data-team-turn]').forEach((button) => {
    button.addEventListener('click', () => {
      const id = button.dataset.teamTurn;
      if (!id) return;
      const values = expandedTurns.get(sessionId) || new Set<string>();
      if (values.has(id)) values.delete(id);
      else values.add(id);
      expandedTurns.set(sessionId, values);
      window.dispatchEvent(new CustomEvent('team-collaboration:updated', { detail: { sessionId } }));
    });
  });
  document.querySelectorAll<HTMLButtonElement>('[data-team-node]').forEach((button) => {
    button.addEventListener('click', () => {
      const id = button.dataset.teamNode;
      if (!id) return;
      const values = expandedNodes.get(sessionId) || new Set<string>();
      if (values.has(id)) values.delete(id);
      else values.add(id);
      expandedNodes.set(sessionId, values);
      window.dispatchEvent(new CustomEvent('team-collaboration:updated', { detail: { sessionId } }));
    });
  });
  document.querySelectorAll<HTMLDetailsElement>('[data-team-execution]').forEach((details) => {
    details.addEventListener('toggle', () => {
      const id = details.dataset.teamExecution;
      if (!id) return;
      const values = openExecutions.get(sessionId) || new Set<string>();
      if (details.open) values.add(id);
      else values.delete(id);
      openExecutions.set(sessionId, values);
    });
  });
  document.querySelectorAll<HTMLButtonElement>('[data-team-open-path]').forEach((button) => {
    button.addEventListener('click', () => {
      const path = button.dataset.teamOpenPath;
      if (path) void openPath(path);
    });
  });
  document.querySelectorAll<HTMLSelectElement>('[data-team-member-model]').forEach((select) => {
    select.addEventListener('change', () => {
      const memberId = select.dataset.teamMemberId;
      const modelId = select.value;
      if (!memberId || !modelId) return;
      select.disabled = true;
      void backendApi.setSessionModel(sessionId, modelId, { member_id: memberId })
        .then((binding) => {
          if (!binding.ok) throw new Error('模型切换未生效');
          notify(`已切换${binding.member_name || '成员'}模型：${binding.model_label || modelId}`);
          snapshots.delete(sessionId);
          return refreshTeamCollaborationBoard(sessionId);
        })
        .catch((error) => {
          notify(`模型切换失败：${error instanceof Error ? error.message : String(error)}`);
          snapshots.delete(sessionId);
          return refreshTeamCollaborationBoard(sessionId);
        });
    });
  });
  document.querySelectorAll<HTMLButtonElement>('[data-team-cancel-task]').forEach((button) => {
    button.addEventListener('click', () => {
      const taskId = button.dataset.teamCancelTask;
      if (!taskId) return;
      button.disabled = true;
      void backendApi.cancelTask(taskId).then(() => refreshTeamCollaborationBoard(sessionId)).catch((error) => {
        button.disabled = false;
        notify(`取消节点失败：${error instanceof Error ? error.message : String(error)}`);
      });
    });
  });
  document.querySelectorAll<HTMLButtonElement>('[data-team-recover-action]').forEach((button) => {
    button.addEventListener('click', () => {
      const action = button.dataset.teamRecoverAction as 'reassign' | 'retry' | 'abandon' | undefined;
      const nodeId = button.dataset.teamRecoverNode;
      if (!action || !nodeId) return;
      const selected = document.querySelector<HTMLSelectElement>(
        `[data-team-recovery-assignee="${CSS.escape(nodeId)}"]`,
      )?.value || button.dataset.teamRecoverAssignee || '';
      button.disabled = true;
      void backendApi.recoverTeamNode(sessionId, nodeId, action, selected)
        .then((result) => {
          if (!result.ok) throw new Error(result.error || '节点恢复失败');
          return refreshTeamCollaborationBoard(sessionId);
        })
        .catch((error) => {
          button.disabled = false;
          notify(`节点恢复失败：${error instanceof Error ? error.message : String(error)}`);
        });
    });
  });
  document.querySelector<HTMLButtonElement>('[data-team-files]')?.addEventListener('click', () => {
    if (filesOpen.has(sessionId)) filesOpen.delete(sessionId);
    else filesOpen.add(sessionId);
    window.dispatchEvent(new CustomEvent('team-collaboration:updated', { detail: { sessionId } }));
  });
  document.querySelector<HTMLButtonElement>('[data-team-close]')?.addEventListener('click', () => {
    document.getElementById('task-board-toggle')?.click();
  });
}
