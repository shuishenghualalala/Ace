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
import type { ChatMessage, ToolCallInfo } from '../chat-render';
import { escapeHtml, notify, state, newMessageId } from '../state';
import { setRuntimeStyle } from '../components/runtime-style';
import {
  backendDurationToMs,
  mergeTeamInternalMessage,
  normalizeTurnFileChanges,
} from './history-mapping';
import {
  featureEventRegistry,
  emptyFeatureReducerResult,
  type FeatureReducerContext,
  type FeatureReducerResult,
} from './event-reducer-registry';
import { registerTeamBoardCallbacks } from './board-hooks';
import { renderMarkdownHtml } from '../markdown';
import { externalAgentTone } from './external-agent-avatar';

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
  hierarchical: boolean;
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
  mentionId?: string;
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
  fast_minimal_path: '快速处理',
  standard_role_dag: '团队协作',
  standard_semantic_dag: '团队协作',
  standard_responsibility_dag: '职责驱动兜底',
  ai_single_dag: '深度规划',
  heavy_single_dag: '深度规划',
  heavy_multi_candidate: '深度规划',
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
const filesOpen = new Set<string>();
const stableProgress = new Map<string, { turnId: string; completed: number; total: number; percent: number }>();
// 请求占用以唯一 token 表示（会话 → 自增序号）：finally 只有在仍占有该 token 时才释放，
// 旧代迟到请求的清理因此不会误删新代同会话请求的占用标记。
const refreshInFlight = new Map<string, number>();
let refreshInFlightSeq = 0;
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

/** 把历史 Team 消息里遗留的 @agent_id 按当前团队名册显示成人类可读名称。 */
export function resolveTeamCollaborationMentionText(
  sessionId: string | null | undefined,
  value: string,
): string {
  if (!sessionId) return value;
  let text = String(value || '');
  for (const member of snapshots.get(sessionId)?.members || []) {
    const id = String(member.agentId || '').trim();
    const name = String(member.name || '').trim();
    if (id && name && id !== name) text = text.replaceAll(`@${id}`, () => `@${name}`);
  }
  return text;
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

function displayAgentName(value: unknown, members: TeamBoardMember[], leaderName: string): string {
  const raw = String(value || '').trim();
  if (!raw) return '';
  if (raw.toLowerCase() === 'leader') return leaderName;
  const member = members.find((candidate) => candidate.agentId === raw || candidate.mentionId === raw || candidate.name === raw);
  return member?.name || raw;
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
  if (progressText(task, 'collaboration_task_id')) {
    const attempts = Array.isArray(task.progress?.execution_attempts) ? task.progress.execution_attempts : [];
    return [
      `执行状态：${progressText(task, 'execution_state') || 'pending'}`,
      `执行尝试 ${attempts.length} 次`,
      ...(task.error ? [String(task.error)] : []),
    ];
  }

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

export function nodeMessageId(node: TeamFlowNode, messages: ChatMessage[]): string {
  const nodeIds = new Set(
    [planNodeId(node), node.raw.task_id, node.raw.id]
      .map((value) => String(value || '').trim())
      .filter(Boolean),
  );
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (message.nodeId && nodeIds.has(String(message.nodeId).trim())) return message.id;
  }
  return '';
}

function parentNodeIds(node: TeamFlowNode): string[] {
  return stringList(node.raw.progress?.parent_node_ids);
}

function assignmentMessageForNode(node: TeamFlowNode, messages: ChatMessage[]): ChatMessage | undefined {
  const ids = new Set([planNodeId(node), node.raw.task_id, node.raw.id].map((value) => String(value || '').trim()).filter(Boolean));
  const turn = String(node.raw.progress?.turn_session_id || node.raw.session_id || '').trim();
  return [...messages].reverse().find((message) => {
    const source = String(message.sourceSessionId || '');
    if (message.eventType !== 'team_assign' || !message.nodeId || !ids.has(message.nodeId)) return false;
    if (source.includes('::turn::')) return turn.includes('::turn::') && (source === turn || source.startsWith(`${turn}::`));
    if (source && (source === turn || source.startsWith(`${turn}::`))) return true;
    return Boolean(turn.includes('::turn::') && message.requestId && turn.endsWith(`::turn::${message.requestId}`));
  });
}

function nodeDispatch(node: TeamFlowNode, messages: ChatMessage[]): { sender: string; recipients: string[]; fromExecution: boolean } | undefined {
  const message = assignmentMessageForNode(node, messages);
  if (message?.mentionFrom && message.mentionTo?.length) {
    return { sender: message.mentionFrom, recipients: message.mentionTo, fromExecution: false };
  }
  if (!progressText(node.raw, 'collaboration_task_id')) return undefined;
  // Completed journal attempts preserve the real executor even if chat events
  // were not retained; the collaboration adapter dispatches as the leader.
  const attempts = Array.isArray(node.raw.progress?.execution_attempts)
    ? node.raw.progress.execution_attempts as Array<Record<string, unknown>> : [];
  const completed = [...attempts].reverse().find((attempt) => attempt.state === 'completed' && String(attempt.actual_executor || '').trim());
  return completed ? { sender: 'leader', recipients: [String(completed.actual_executor).trim()], fromExecution: true } : undefined;
}

function executionWorkerIds(node: TeamFlowNode): string[] {
  const attempts = Array.isArray(node.raw.progress?.execution_attempts)
    ? node.raw.progress.execution_attempts as Array<Record<string, unknown>>
    : [];
  const actual = attempts.map((attempt) => String(attempt.actual_executor || '').trim()).filter(Boolean);
  if (actual.length) return [...new Set(actual)];
  return node.owner && node.owner !== '待分配' ? [node.owner] : [];
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
  if (!hasPlanNodes) return nodes.length ? [{ id: 'flow_nodes', depth: 0, hierarchical: false, nodes }] : [];
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
      hierarchical: true,
      nodes: stageNodes.sort((a, b) => displayRank(a.raw) - displayRank(b.raw) || a.title.localeCompare(b.title)),
    }));
}

function renderFlowStage(
  sessionId: string,
  stage: TeamFlowStage,
  nodes: TeamFlowNode[],
  messages: ChatMessage[],
  leaderName: string,
  members: TeamBoardMember[],
): string {
  const countLabel = `${stage.nodes.length} 个任务`;
  const groups = new Map<string, TeamFlowNode[]>();
  for (const node of stage.nodes) groups.set(node.owner, [...(groups.get(node.owner) || []), node]);
  const ids = new Set(nodes.flatMap((node) => [planNodeId(node), node.raw.task_id, node.raw.id]).filter(Boolean));
  const unresolved = stage.nodes.some((node) => parentNodeIds(node).some((id) => !ids.has(id)));
  const title = !stage.hierarchical ? '任务节点' : `流程 ${stage.depth + 1}`;
  const concurrency = unresolved ? '上游节点待确认' : stage.hierarchical && groups.size > 1 ? `可并行 · ${groups.size} 位成员` : `${stage.nodes.length} 个节点`;
  const dispatches = new Map<string, TeamFlowNode[]>();
  for (const node of stage.nodes) {
    const evidence = nodeDispatch(node, messages);
    if (!evidence) continue;
    const sender = displayAgentName(evidence.sender, members, leaderName);
    const recipients = evidence.recipients.map((id) => displayAgentName(id, members, leaderName)).filter((name) => name && name !== sender);
    if (!recipients.length) continue;
    dispatches.set(sender, [...(dispatches.get(sender) || []), node]);
  }
  const dispatchHtml = [...dispatches].map(([sender, dispatched]) => `<div class="flow-dispatch"><div class="flow-dispatch__head"><strong>${escapeHtml(sender)} · 派发任务</strong><span>${dispatched.length} 项</span></div><ul class="flow-dispatch__tasks">${dispatched.map((node) => `<li title="${htmlAttr(node.fullTitle || node.title)}">${escapeHtml(node.title)}</li>`).join('')}</ul></div>`).join('');
  const lanes = [...groups].map(([owner, memberNodes]) => `<div class="flow-member-lane"><div class="flow-member-lane__head">${escapeHtml(displayAgentName(owner, members, leaderName))} · ${memberNodes.length} 项</div>${memberNodes.map((node) => renderNode(sessionId, node, nodes, messages, leaderName, members)).join('')}</div>`).join('');
  return `<section class="flow-lane is-${stageStatus(stage)}" aria-label="${title}，${countLabel}"><header class="flow-lane__head"><strong>${title}</strong><span>${escapeHtml(concurrency)}</span></header>${dispatchHtml}<div class="flow-lane__nodes">${lanes}</div></section>`;
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
      status: stageStatus({ id: `${id}:all`, depth: -1, hierarchical: false, nodes: groupNodes }),
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
  return [...new Set(parents.map((id) => byId.has(id)
    ? compactText(byId.get(id) || '', 18)
    : '上游节点'))].join('、');
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

export function durationLabel(task: Task, now = Date.now() / 1000): string {
  const attempts = Array.isArray(task.progress?.execution_attempts) ? task.progress.execution_attempts as Array<Record<string, unknown>> : [];
  const latest = attempts[attempts.length - 1];
  const start = latest ? latest.started_at : task.started_at;
  const running = normalizeTeamFlowStatus(task) === 'running';
  const end = latest ? latest.ended_at : task.finished_at;
  if (typeof start !== 'number' || !Number.isFinite(start) || start <= 0) return '';
  const hasEnd = typeof end === 'number' && Number.isFinite(end) && end > 0;
  const finish = hasEnd ? end : running ? now : 0;
  if (!finish || finish < start) return '';
  const seconds = Math.max(0, Math.round(finish - start));
  const prefix = running && !hasEnd ? '已运行 ' : '耗时 ';
  if (seconds < 1) return `${prefix}<1s`;
  if (seconds < 60) return `${prefix}${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  return prefix + (seconds % 60 ? `${minutes}m ${seconds % 60}s` : `${minutes}m`);
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
  return ordered.map((member) => {
    const isLeader = member.agent_id === leaderId;
    const isBuiltin = member.agent_id === CREW_BUILTIN_AGENT_ID;
    const model = modelByMemberId.get(member.agent_id);
    return {
      agentId: member.agent_id,
      mentionId: isLeader ? 'leader' : member.agent_name || agentNames.get(member.agent_id) || member.agent_id,
      name: isBuiltin ? 'Crew' : member.agent_name || agentNames.get(member.agent_id) || member.agent_id,
      displayBadge: member.display_badge || (isBuiltin ? 'M' : '?'),
      role: isLeader
        ? summarizeLeaderRole(member.role || '')
        : `负责${summarizeBusinessDuty(member.role || '', '协作执行', { max: 80 })}`,
      isLeader,
      tone: isLeader ? 0 : externalAgentTone(member.agent_provider || agents.find((agent) => agent.id === member.agent_id)?.provider || member.agent_name || member.agent_id),
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
  if (!teamId) {
    if (config?.executor !== 'team') return { name: '', members: [] };
    const journal = await backendApi.collaborationExecutions(sessionId).catch(() => null);
    const snapshot = journal?.executions.at(-1)?.team_snapshot;
    return { name: snapshot?.name || '内置协作小队',
      members: (snapshot?.members || []).map((member, index) => ({
        agentId: member.member_id, mentionId: member.member_id === snapshot?.leader_id ? 'leader' : member.name,
        name: member.name, role: member.responsibility,
        isLeader: member.member_id === snapshot?.leader_id, tone: index % 6,
      })) };
  }
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
  const generation = teamBoardGeneration;
  const configured = await loadConfiguredTeam(sessionId);
  // 在途守卫：预热期间看板被卸载/重装时，迟到响应不写入快照。
  if (generation !== teamBoardGeneration) return;
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
  const token = ++refreshInFlightSeq;
  refreshInFlight.set(sessionId, token);
  const generation = teamBoardGeneration;
  try {
    const previous = snapshots.get(sessionId);
    const [tasks, runtime, configured] = await Promise.all([
      backendApi.tasks(sessionId).catch(() => previous?.tasks || []),
      backendApi.runtimeConcurrency().catch(() => previous?.runtime || null),
      previous?.members.length && previous.teamName
        ? Promise.resolve({ name: previous.teamName, members: previous.members })
        : loadConfiguredTeam(sessionId),
    ]);
    // 在途守卫：请求期间看板被卸载/重装（代际变化）时，迟到响应整体丢弃
    // —— 不写 snapshots/stableNodes、不派发更新事件（finally 仍按 token 释放自己的占用）。
    if (generation !== teamBoardGeneration) return;
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
    // token 所有权：只有 map 中仍是自己那个 token 时才允许释放，
    // 旧代不得清除新代同会话请求的占用（否则后续刷新会并发执行、乱序回写）。
    if (refreshInFlight.get(sessionId) === token) {
      refreshInFlight.delete(sessionId);
    }
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
  disposeTeamCollaborationBoard();
  resetTeamCollaborationState();
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
  return `<details class="team-member__model"><summary>模型</summary><label title="${htmlAttr(disabled ? reason : '切换该成员模型')}"><select class="team-member__model-select" data-team-member-model data-team-member-id="${htmlAttr(member.agentId || '')}" aria-label="${htmlAttr(`${member.name}模型`)}"${disabled ? ' disabled' : ''}>${options}</select></label></details>`;
}

function renderMembers(members: TeamBoardMember[]): string {
  return members.map((member) => `
    <div class="team-member${member.isLeader ? ' is-leader' : ''}">
      ${member.agentId === CREW_BUILTIN_AGENT_ID
        ? crewAvatarHtml()
        : `<span class="agent-avatar agent-tone-${member.tone}">${escapeHtml(member.displayBadge || '?')}</span>`}
      <div class="team-member__copy"><strong>${escapeHtml(member.name)}${member.isLeader ? '<span class="pixel-flag" aria-label="Leader" title="Leader"></span>' : ''}</strong><p title="${htmlAttr(member.role)}">${escapeHtml(member.role)}</p>${memberModelHtml(member)}</div>
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

function collaborationControls(node: TeamFlowNode): string {
  const task = progressText(node.raw, 'collaboration_task_id');
  const unit = planNodeId(node);
  const status = progressText(node.raw, 'execution_state');
  const taskStatus = progressText(node.raw, 'execution_task_state');
  const button = (action: string, label: string) => `<button class="mini-cancel" type="button" data-collab-task="${htmlAttr(task)}" data-collab-unit="${htmlAttr(unit)}" data-collab-action="${action}">${label}</button>`;
  const controls = taskStatus === 'paused' ? button('resume', '恢复任务')
    : taskStatus === 'running' ? button('pause', '暂停后续工作') + button('cancel', '取消任务') + (status === 'running' ? button('reconcile', '核对中断状态') : '')
    : status === 'failed' ? button('retry', '原成员重试')
    : status === 'unknown' ? button('retry_unknown', '核实后重试') : '';
  return `<div class="flow-node__actions"><span>${status === 'unknown' ? '外部结果待核实，禁止自动重派' : '成果待人工复核'}</span>${controls}</div>`;
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
  const displayAgent = displayAgentName(node.owner, members, leaderName);
  const executionAttempts = Array.isArray(node.raw.progress?.execution_attempts)
    ? node.raw.progress.execution_attempts as Array<Record<string, unknown>>
    : [];
  const actualWorkers = executionAttempts.map((attempt) => String(attempt.actual_executor || '').trim()).filter(Boolean);
  const workerNames = [...new Set(actualWorkers.length ? actualWorkers : (node.owner && node.owner !== '待分配' ? [node.owner] : []))]
    .map((member) => displayAgentName(member, members, leaderName));
  const duration = durationLabel(node.raw);
  const handoff = `<div class="flow-node__handoff"><span class="flow-node__worker-count">执行成员 ${workerNames.length} 位 · ${escapeHtml(workerNames.join('、') || '待分配')}</span>${duration ? `<span class="flow-node__duration">${escapeHtml(duration)}</span>` : ''}</div>`;
  const previousAssignee = String(node.raw.progress?.previous_assignee || '').trim();
  const assignmentDetail = node.owner === '待分配' && previousAssignee
    ? `<span>原主责 ${escapeHtml(displayAgentName(previousAssignee, members, leaderName))}</span>`
    : '';
  if (!isExpanded) {
    return `<article class="flow-node is-${node.status}"><div class="flow-node__card"><button class="flow-node__summary-btn" type="button" data-team-node="${htmlAttr(node.id)}" aria-expanded="false"><div class="flow-node__top"><span class="agent-chip">主责：${escapeHtml(displayAgent)}</span><span class="flow-status is-${node.status}">${statusLabel[node.status]}</span></div><strong class="flow-node__title" title="${htmlAttr(node.fullTitle || node.title)}">${escapeHtml(node.title)}</strong>${handoff}${dependency ? `<span class="flow-node__dependency">依赖 ← ${escapeHtml(dependency)}</span>` : ''}<span class="flow-node__hint">点击展开详情</span></button></div></article>`;
  }
  const paths = artifactPaths(node.raw);
  const messageId = nodeMessageId(node, messages);
  const assignmentReason = progressText(node.raw, 'assignment_reason');
  const responsibilityQuote = progressText(node.raw, 'responsibility_quote');
  const capabilityStatus = progressText(node.raw, 'capability_status');
  const fallbackFrom = progressText(node.raw, 'planning_fallback_from');
  const failureType = progressText(node.raw, 'planning_failure_type');
  const planningContext = [
    progressText(node.raw, 'planning_explanation') ? `规划依据：${progressText(node.raw, 'planning_explanation')}` : '',
    assignmentReason ? `分配依据：${assignmentReason}` : '',
    responsibilityQuote ? `用户职责：${responsibilityQuote}` : '',
    capabilityStatus && capabilityStatus !== 'covered' ? `能力状态：${capabilityStatus}；所需能力：${progressStringList(node.raw, 'required_capabilities').join('、') || '待确认'}` : '',
    fallbackFrom ? `规划使用备用路径${failureType ? `（${failureType}）` : ''}` : '',
  ].filter(Boolean);
  const summaryItems = node.summaryItems.length ? node.summaryItems : [node.summary];
  const recoveryMembers = members.filter((member) => !member.isLeader && member.name);
  const recoveryNodeId = planNodeId(node);
  const leaderReview = node.owner.toLowerCase() === 'leader' && (recoveryNodeId === 'leader_review' || recoveryNodeId.startsWith('leader_review_'));
  const collab = progressText(node.raw, 'collaboration_task_id');
  const recoveryActions = collab ? collaborationControls(node) : node.status === 'blocked'
    ? `<div class="flow-node__actions flow-node__recovery-actions" data-team-recover-workflow="${htmlAttr(progressText(node.raw, 'workflow_id'))}"><span>阻塞节点处理</span>${node.raw.progress?.can_confirm_original === true ? `<button class="mini-cancel" type="button" data-team-recover-action="confirm_attempt" data-team-recover-node="${htmlAttr(recoveryNodeId)}">确认原成员尝试</button>` : ''}${leaderReview ? `<button class="mini-cancel" type="button" data-team-recover-action="retry" data-team-recover-node="${htmlAttr(recoveryNodeId)}" data-team-recover-assignee="leader">重试审阅</button>` : previousAssignee ? `<button class="mini-cancel" type="button" data-team-recover-action="retry" data-team-recover-node="${htmlAttr(recoveryNodeId)}" data-team-recover-assignee="${htmlAttr(previousAssignee)}">重试原成员</button>` : ''}${!leaderReview && recoveryMembers.length ? `<select class="mini-recovery-select" data-team-recovery-assignee="${htmlAttr(recoveryNodeId)}" aria-label="选择恢复成员">${recoveryMembers.map((member) => `<option value="${htmlAttr(member.name)}">${escapeHtml(member.name)}</option>`).join('')}</select><button class="mini-cancel" type="button" data-team-recover-action="reassign" data-team-recover-node="${htmlAttr(recoveryNodeId)}">重新分配</button>` : ''}${!leaderReview ? `<button class="mini-cancel" type="button" data-team-recover-action="abandon" data-team-recover-node="${htmlAttr(recoveryNodeId)}">放弃节点</button>` : ''}</div>`
    : '';
  return `<article class="flow-node is-${node.status}"><div class="flow-node__card">
    <button class="flow-node__summary-btn" type="button" data-team-node="${htmlAttr(node.id)}" aria-expanded="true"><div class="flow-node__top"><span class="agent-chip">主责：${escapeHtml(displayAgent)}</span><span class="flow-status is-${node.status}">${statusLabel[node.status]}</span></div><strong class="flow-node__title" title="${htmlAttr(node.fullTitle || node.title)}">${escapeHtml(node.title)}</strong>${handoff}${dependency ? `<span class="flow-node__dependency">依赖 ← ${escapeHtml(dependency)}</span>` : ''}<span class="flow-node__hint">收起详情</span></button>
    <div class="flow-node__detail">
      <section class="flow-node__brief" aria-label="节点摘要"><div class="flow-node__meta-line"><span>负责人 ${escapeHtml(displayAgent)}</span>${assignmentDetail}<span>${statusLabel[node.status]}</span>${duration ? `<span>${duration}</span>` : ''}</div>${node.fullTitle && node.fullTitle !== node.title ? `<p class="flow-node__full-title">节点任务：${escapeHtml(node.fullTitle)}</p>` : ''}<ul class="flow-node__brief-list">${summaryItems.map((item) => `<li>${escapeHtml(item)}</li>`).join('')}</ul></section>
      ${executionAttempts.length ? `<details class="flow-node__attempts"><summary>执行记录 · ${executionAttempts.length} 次</summary><ol>${executionAttempts.map((attempt, index) => `<li>第 ${index + 1} 次 · ${escapeHtml(String(attempt.state || 'unknown'))}${attempt.planned_member ? ` · 计划 ${escapeHtml(displayAgentName(attempt.planned_member, members, leaderName))}` : ''}${attempt.actual_executor ? ` · 实际 ${escapeHtml(displayAgentName(attempt.actual_executor, members, leaderName))}` : ' · 未回传执行者'}${attempt.error ? ` · ${escapeHtml(String(attempt.error))}` : ''}</li>`).join('')}</ol></details>` : ''}
      ${planningContext.length ? `<details class="flow-node__context"><summary>规划与分配依据</summary><ul>${planningContext.map((item) => `<li>${escapeHtml(item)}</li>`).join('')}</ul></details>` : ''}
      ${collab && node.raw.result ? `<details class="flow-node__record"><summary>完整工作记录</summary><section class="flow-node__result" aria-label="完整协作成果"><h3>${node.status === 'completed' ? '完整成果（待人工复核）' : '未通过验收的原始输出'}</h3><div class="md-body chat-markdown">${renderMarkdownHtml(String(node.raw.result))}</div></section></details>` : ''}
      ${paths.length ? `<section class="flow-node__artifacts"><span>产物</span>${paths.map((path) => `<button type="button" data-team-open-path="${htmlAttr(path)}" title="${htmlAttr(path)}"><i class="flow-node__file-icon" aria-hidden="true"></i><strong>${escapeHtml(fileNameOf(path))}</strong></button>`).join('')}</section>` : ''}
      ${messageId ? `<div class="flow-node__actions flow-node__locate-actions"><button class="mini-cancel flow-node__locate" type="button" data-team-locate-message="${htmlAttr(messageId)}">定位</button></div>` : ''}
      ${recoveryActions}${!collab && ['pending', 'running'].includes(node.raw.status) ? `<div class="flow-node__actions"><button class="mini-cancel" type="button" data-team-cancel-task="${htmlAttr(node.raw.task_id || node.raw.id)}">取消节点</button></div>` : ''}
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
  return `<section class="runtime-box"><div class="runtime-box__head"><span>运行状态</span><span>${runtime ? `${runtime.global_active}/${runtime.max_active_runs || '∞'}` : '—'}</span></div><div class="runtime-box__current"><span title="${htmlAttr(sessionId)}">${escapeHtml(sessionId)}</span><em title="${htmlAttr(dagInfo?.detail || '等待团队任务')}">${dagInfo ? `${escapeHtml(dagInfo.label)} · ${escapeHtml(dagInfo.detail)}` : '等待团队任务'}</em></div><div class="runtime-box__grid"><div><span>全局排队</span><b>${runtime?.global_queued ?? 0}</b></div><div><span>活跃子任务</span><b>${activeChildCount}</b></div></div>${sessions.length ? `<div class="runtime-box__sessions">${sessions.slice(0, 4).map((item) => `<div class="runtime-session"><span title="${htmlAttr(item.session_id)}">${escapeHtml(item.session_id || '')}</span><em>${escapeHtml(item.live || '空闲')}${item.queue_depth ? ` · 排队 ${item.queue_depth}` : ''}</em></div>`).join('')}</div>` : '<div class="runtime-box__empty">请求队列为空；恢复任务请以上方执行日志为准</div>'}</section>`;
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
  const latestTurnId = turns.at(-1)?.id;
  if (latestTurnId && !known.has(latestTurnId)) expanded.add(latestTurnId);
  for (const id of [...expanded]) if (!currentIds.has(id)) expanded.delete(id);
  knownTurns.set(sessionId, currentIds);
  expandedTurns.set(sessionId, expanded);

  const completed = currentNodes.filter((node) => node.status === 'completed').length;
  const runningNode = currentNodes.find((node) => node.status === 'running');
  const authoritative = currentNodes.some((node) => Boolean(progressText(node.raw, 'collaboration_task_id')));
  const rawProgress = currentNodes.length ? Math.min(100, Math.round(((completed + (!authoritative && runningNode ? 0.35 : 0)) / currentNodes.length) * 100)) : 0;
  const previousProgress = stableProgress.get(sessionId);
  const progress = !authoritative && previousProgress?.turnId === currentTurnId
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
  const dependencyCount = currentNodes.reduce((count, node) => count + parentNodeIds(node).length, 0);
  const dagInfo = currentNodes.length ? {
    label: dagStrategyLabel[strategy] || ({ fast: '快速协作', standard: '标准协作', ai: '深度协作' }[tier] || '团队流程图'),
    detail: [`${currentNodes.length} 个节点`, dependencyCount ? `${dependencyCount} 条依赖连接` : '并行流程'].join(' · '),
  } : null;

  const flowHtml = nodes.length ? `<section class="flow-map" aria-label="团队流程图">${turns.map((turn, turnIndex) => `<section class="flow-turn is-${turn.status}"><button class="flow-turn__head" type="button" data-team-turn="${htmlAttr(turn.id)}" aria-expanded="${expanded.has(turn.id)}"><span>第 ${turnIndex + 1} 轮</span><div><strong>${escapeHtml(turn.title)}</strong><em>${statusLabel[turn.status]}</em></div></button>${expanded.has(turn.id) ? turn.stages.map((stage) => renderFlowStage(sessionId, stage, turn.stages.flatMap((item) => item.nodes), state.messages[sessionId] || [], leaderName, members)).join('') : ''}</section>`).join('')}</section>` : `<div class="board__empty board__empty--cute"><span class="pixel-empty" aria-hidden="true"></span><strong>${snapshot.loaded ? '还没有流程节点' : '正在加载协作流程'}</strong><p>${snapshot.loaded ? '开始任务后，这里会按依赖关系展示节点、执行者与交接方向。' : '正在读取团队成员、任务节点和运行状态。'}</p></div>`;

  return `<div class="inspector-team-collaboration"><aside class="team-collaboration-board">
    <div class="board__head"><div><span class="board__title-row">协作看板<button class="board-files-btn${filesOpen.has(sessionId) ? ' is-active' : ''}" type="button" data-team-files aria-expanded="${filesOpen.has(sessionId)}" title="查看当前 session 文件清单">${folderIconHtml()}${files.length ? `<i>${files.length}</i>` : ''}</button></span><em>任务流转</em></div><div class="board__actions"><button class="team-board-close" type="button" data-team-close title="收起">×</button></div></div>
    ${renderFiles(sessionId, files)}
    <div class="board__list team-board__list">
      <section class="flow-hero"><div class="flow-hero__title">${teamMarkHtml()}<div><strong>${nodes.length ? '团队流程图' : '等待团队流程'}</strong><p>${nextNode ? (allCompleted ? `已完成：${escapeHtml(nextNode.title)}` : `当前节点：${escapeHtml(nextNode.title)}`) : '流程图会显示依赖、派出方、接收方与执行人数。'}</p></div></div><div class="flow-meter" aria-label="完成进度 ${progress.percent}%"><span data-team-progress="${progress.percent}"></span></div><div class="flow-hero__meta"><span>${progress.completed}/${progress.total || 0} 已完成</span><span>${runningNode ? `${escapeHtml(displayAgentName(runningNode.owner, members, leaderName))} 处理中` : '暂无运行节点'}</span></div><div class="team-roster__head"><strong>团队成员</strong><span>${members.length} 位</span></div><div class="team-members" aria-label="团队成员">${renderMembers(members)}</div></section>
      ${flowHtml}
      ${errors.length ? `<section class="board-alert"><strong>错误待处理</strong><p>${escapeHtml(errors[0]?.title || '')}：${escapeHtml(errors[0]?.summary || '')}</p></section>` : ''}
      <details class="team-runtime-details"><summary>运行详情</summary>${renderRuntime(sessionId, snapshot.runtime, dagInfo)}</details>
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
  document.querySelectorAll<HTMLButtonElement>('[data-team-locate-message]').forEach((button) => {
    button.addEventListener('click', () => {
      const messageId = button.dataset.teamLocateMessage;
      if (!messageId) return;
      const target = Array.from(document.querySelectorAll<HTMLElement>('[data-message-id]'))
        .find((element) => element.dataset.messageId === messageId);
      if (!target) {
        notify('暂时找不到对应的团队消息');
        return;
      }
      target.scrollIntoView({ behavior: 'smooth', block: 'center' });
      target.classList.add('msg--located');
      window.setTimeout(() => target.classList.remove('msg--located'), 1_400);
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
  document.querySelectorAll<HTMLButtonElement>('[data-collab-action]').forEach((button) => {
    button.addEventListener('click', () => {
      const action = button.dataset.collabAction || '';
      const unknown = action === 'retry_unknown';
      if (unknown && !window.confirm('外部调用结果未知。你是否已检查实际执行记录及副作用，并确认允许再次执行？')) return;
      button.disabled = true;
      void backendApi.controlCollaboration(button.dataset.collabTask || '', unknown ? 'retry' : action,
        button.dataset.collabUnit || '', unknown)
        .then(() => refreshTeamCollaborationBoard(sessionId))
        .catch((error) => { button.disabled = false; notify(`操作失败：${String(error)}`); });
    });
  });
  document.querySelectorAll<HTMLButtonElement>('[data-team-recover-action]').forEach((button) => {
    button.addEventListener('click', () => {
      const action = button.dataset.teamRecoverAction as 'reassign' | 'retry' | 'abandon' | 'confirm_attempt' | undefined;
      const nodeId = button.dataset.teamRecoverNode;
      if (!action || !nodeId) return;
      const selected = document.querySelector<HTMLSelectElement>(
        `[data-team-recovery-assignee="${CSS.escape(nodeId)}"]`,
      )?.value || button.dataset.teamRecoverAssignee || '';
      button.disabled = true;
      void backendApi.recoverTeamNode(sessionId, nodeId, action, selected, button.closest<HTMLElement>('[data-team-recover-workflow]')?.dataset.teamRecoverWorkflow || '')
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

function backendSecondsToMs(value: unknown, fallback = Date.now()): number {
  const number = typeof value === 'number' ? value : Number(value);
  if (!Number.isFinite(number) || number <= 0) return fallback;
  return number < 1_000_000_000_000 ? number * 1000 : number;
}

function normalizeTeamToolCalls(raw: unknown): ToolCallInfo[] | undefined {
  if (!Array.isArray(raw)) return undefined;
  const calls = raw.map((item, index): ToolCallInfo => {
    const value = item && typeof item === 'object' ? item as Record<string, unknown> : {};
    const status = value.status === 'running' || value.status === 'generating' || value.status === 'error'
      ? value.status
      : 'done';
    const args = typeof value.arguments === 'string'
      ? value.arguments
      : JSON.stringify(value.arguments || {});
    return {
      toolCallId: String(value.id || value.tool_call_id || `team_tool_${index}`),
      name: String(value.name || 'unknown'),
      ...(typeof value.ui_label === 'string' ? { uiLabel: value.ui_label } : {}),
      args,
      ...(typeof value.result === 'string' ? { result: value.result } : {}),
      status,
      startedAt: backendSecondsToMs(value.started_at, 0),
      ...(typeof value.duration === 'number' && backendDurationToMs(value.duration) > 0
        ? { duration: backendDurationToMs(value.duration) }
        : {}),
    };
  });
  return calls.length ? calls : undefined;
}

function normalizeTeamText(value: unknown): string {
  if (value == null) return '';
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  if (typeof value === 'object') {
    const record = value as Record<string, unknown>;
    for (const key of ['message', 'text', 'content', 'summary']) {
      if (typeof record[key] === 'string' && String(record[key]).trim()) return String(record[key]);
    }
    try { return JSON.stringify(value); } catch { return ''; }
  }
  return String(value);
}

export interface TeamInternalBody {
  text?: string;
  source_session_id?: string;
  agent_id?: string;
  agent_name?: string;
  agent_role?: string;
  agent_tone?: number;
  is_leader?: boolean;
  event_type?: string;
  node_id?: string;
  mention_from?: string;
  mention_to?: string[];
  mention_intent?: string;
  communication_kind?: string;
  communication_status?: string;
  request_id?: string;
  reply_to?: string;
  communication_request_text?: string;
  display_mode?: string;
  collapsed_title?: string;
  process_text?: string;
  artifacts?: ChatMessage['artifacts'];
  turn_file_changes?: ChatMessage['turnFileChanges'];
  thinking?: unknown;
  tool_calls?: unknown[];
  turn_started_at?: number;
  turn_duration?: number;
  timestamp?: number;
  append?: boolean;
}

function buildTeamInternalMessage(body: TeamInternalBody, ctx: FeatureReducerContext): ChatMessage {
  const timestamp = backendSecondsToMs(body.timestamp);
  const toolCalls = normalizeTeamToolCalls(body.tool_calls);
  const turnFileChanges = normalizeTurnFileChanges(body.turn_file_changes);
  const streaming = body.display_mode === 'stream' || body.event_type === 'team_stream';
  return {
    id: newMessageId('team'),
    role: 'team_internal',
    content: typeof body.text === 'string' ? body.text : '',
    timestamp,
    segmentRole: 'answer',
    streaming,
    ...(typeof body.source_session_id === 'string' ? { sourceSessionId: body.source_session_id } : {}),
    ...(typeof body.agent_id === 'string' ? { agentId: body.agent_id } : {}),
    ...(typeof body.agent_name === 'string' ? { agentName: body.agent_name } : {}),
    ...(typeof body.agent_role === 'string' ? { agentRole: body.agent_role } : {}),
    ...(typeof body.agent_tone === 'number' ? { agentTone: body.agent_tone } : {}),
    ...(typeof body.is_leader === 'boolean' ? { isLeader: body.is_leader } : {}),
    ...(typeof body.event_type === 'string' ? { eventType: body.event_type } : {}),
    ...(typeof body.node_id === 'string' ? { nodeId: body.node_id } : {}),
    ...(typeof body.mention_from === 'string' ? { mentionFrom: body.mention_from } : {}),
    ...(Array.isArray(body.mention_to) ? { mentionTo: body.mention_to.map(String) } : {}),
    ...(typeof body.mention_intent === 'string' ? { mentionIntent: body.mention_intent } : {}),
    ...(typeof body.communication_kind === 'string' ? { communicationKind: body.communication_kind } : {}),
    ...(typeof body.communication_status === 'string' ? { communicationStatus: body.communication_status } : {}),
    ...(typeof body.request_id === 'string' ? { requestId: body.request_id } : {}),
    ...(typeof body.reply_to === 'string' ? { replyTo: body.reply_to } : {}),
    ...(typeof body.communication_request_text === 'string'
      ? { communicationRequestText: body.communication_request_text }
      : {}),
    ...(typeof body.display_mode === 'string' ? { displayMode: body.display_mode } : {}),
    ...(typeof body.collapsed_title === 'string' ? { collapsedTitle: body.collapsed_title } : {}),
    ...(typeof body.process_text === 'string' ? { processText: body.process_text } : {}),
    ...(Array.isArray(body.artifacts) ? { artifacts: body.artifacts } : {}),
    ...(turnFileChanges ? { turnFileChanges } : {}),
    ...(body.thinking != null ? { thinking: normalizeTeamText(body.thinking) } : {}),
    ...(toolCalls ? { toolCalls } : {}),
    ...(typeof body.turn_started_at === 'number'
      ? { turnStartedAt: backendSecondsToMs(body.turn_started_at) }
      : streaming ? { turnStartedAt: Date.now() } : {}),
    ...(typeof body.turn_duration === 'number' ? { turnDurationMs: Math.max(0, body.turn_duration * 1000) } : {}),
  };
}

export function teamInternalReducer(body: TeamInternalBody, ctx: FeatureReducerContext): FeatureReducerResult {
  const assistantId = ctx.book.assistantId;
  let messages = ctx.messages;
  const bookPatch: Partial<FeatureReducerContext['book']> = { hadTeamInternal: true };

  if (!ctx.book.hadTeamInternal && assistantId) {
    const assistant = messages.find((m) => m.id === assistantId);
    const isEmptyOptimistic = assistant?.role === 'assistant'
      && !assistant.content.trim()
      && !assistant.thinking?.trim()
      && !assistant.toolCalls?.length;
    if (isEmptyOptimistic) {
      messages = messages.filter((m) => m.id !== assistantId);
      bookPatch.assistantId = null;
      bookPatch.firstChunkAt = null;
    }
  }

  const incoming = buildTeamInternalMessage(body, ctx);
  const merged = mergeTeamInternalMessage(messages, incoming, { append: body.append === true });

  return {
    messageUpserts: [],
    toolUpserts: [],
    replaceMessages: merged,
    replaceBook: { ...ctx.book, ...bookPatch },
    statusHint: 'running',
    queueHint: '',
    finalize: false,
  };
}

let teamInitDisposer: (() => void) | null = null;

// 安装代计数：initTeamCollaborationBoard 装新一代、dispose/测试重置销毁一代时各 +1。
// 异步刷新/预热发起前快照当前代，返回后若代已变则整体丢弃，
// 保证卸载/重装后的迟到响应不写 snapshots、不派发更新事件。
// 会话隔离仍由 snapshots 按会话键控承担：迟到响应只允许写回自己那个会话的缓存。
let teamBoardGeneration = 0;

function resetTeamCollaborationState(): void {
  // 代际 +1：使本代所有在途请求在 await 检查点失效。
  teamBoardGeneration += 1;
  stopTeamCollaborationPolling();
  snapshots.clear();
  stableNodes.clear();
  expandedTurns.clear();
  knownTurns.clear();
  expandedNodes.clear();
  filesOpen.clear();
  stableProgress.clear();
  refreshInFlight.clear();
}

/**
 * 单 Feature 安装事务：安装步骤即时收集本次获得的 disposer，
 * 任一步失败则逆序撤销已收集的贡献（每个清理单独兜底，一个失败不阻断其余）；
 * 成功后发布的 disposer 复用同一条清理链，且幂等（二次调用 no-op，
 * 不重复清理、不造成代际语义漂移）。
 */
function createTeamBoardInstallTransaction(scope: string): {
  collect: (disposer: () => void) => void;
  rollback: () => void;
  publish: (finalize: () => void) => () => void;
} {
  const contributions: Array<() => void> = [];
  const undoAll = (): void => {
    for (let index = contributions.length - 1; index >= 0; index -= 1) {
      try {
        contributions[index]?.();
      } catch (err) {
        console.warn(`[${scope}] 安装贡献清理失败（已跳过，继续其余清理）`, err);
      }
    }
  };
  return {
    collect: (disposer) => {
      contributions.push(disposer);
    },
    rollback: undoAll,
    publish: (finalize) => {
      let disposed = false;
      return (): void => {
        if (disposed) return;
        disposed = true;
        undoAll();
        finalize();
      };
    },
  };
}

/**
 * 初始化 Team Collaboration 看板生命周期：
 * - 注册 feature event reducer（team/internal_message@1）；
 * - 向 board-hooks 注册中心调用回调。
 * 返回的 disposer 与 disposeTeamCollaborationBoard() 共享同一条清理路径。
 */
export function initTeamCollaborationBoard(): () => void {
  if (teamInitDisposer) return teamInitDisposer;

  // 新一代安装：与上一代（含其在途请求）彻底隔离。
  // 幂等早退不递增代际；安装失败的回滚不额外递增（本次开始时已递增过，
  // 该代的在途请求已全部失效），避免代际语义漂移。
  teamBoardGeneration += 1;

  const tx = createTeamBoardInstallTransaction('team-collaboration-board');
  try {
    tx.collect(featureEventRegistry.register({
      feature: 'team',
      event: 'internal_message',
      version: 1,
      reducer: (payload, ctx) => teamInternalReducer(payload as TeamInternalBody, ctx),
    }));
    tx.collect(registerTeamBoardCallbacks({
      primeTeamIdentity: primeTeamCollaborationIdentity,
    }));
  } catch (err) {
    // 中途失败：逆序撤销本次已获得的贡献，安装前已存在的贡献不受影响，
    // teamInitDisposer 保持 null（本次安装视为未发生），重试可干净重装。
    tx.rollback();
    throw err;
  }

  teamInitDisposer = tx.publish(resetTeamCollaborationState);
  return teamInitDisposer;
}

/** 显式销毁 Team Collaboration 看板：撤销 reducer、注销回调、释放轮询与状态。 */
export function disposeTeamCollaborationBoard(): void {
  teamInitDisposer?.();
  teamInitDisposer = null;
}
