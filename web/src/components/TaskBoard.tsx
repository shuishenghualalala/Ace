import { useEffect, useMemo, useState, type PointerEvent as ReactPointerEvent } from "react";
import { api } from "../api";
import artifactsFolderIcon from "../assets/team-artifacts-folder-icon.svg";
import { openLocalPath } from "../lib/localPath";
import { compactTaskSummary, compactTaskSummaryItems, compactTaskText, plainTaskText } from "../lib/taskSummary";
import type { Mode, RuntimeConcurrency, Session, Task, TeamMemberView, UiMessage } from "../types";
import AgentAvatarLogo from "./AgentAvatarLogo";

const CREW_BUILTIN_AGENT_ID = "crew::builtin";

interface Props {
  sessionId: string;
  tasks: Task[];
  mode: Mode;
  messages: UiMessage[];
  currentAgentLabel?: Session["agent_label"];
  teamMembers?: TeamMemberView[];
  boardWidth: number;
  onBoardWidthChange: (width: number) => void;
  onJumpToMessage?: (messageId: string) => void;
  onClose: () => void;
  onCancel: (taskId: string) => Promise<void>;
  onRecover: (
    nodeId: string,
    action: "reassign" | "retry" | "abandon",
    replacementAssignee?: string,
  ) => Promise<void>;
}

type FlowStatus = "pending" | "running" | "blocked" | "completed" | "failed" | "cancelled";

export interface FlowNode {
  id: string;
  title: string;
  fullTitle?: string;
  owner: string;
  agents: string[];
  status: FlowStatus;
  summary: string;
  summaryItems: string[];
  raw: Task;
}

interface FlowStage {
  id: string;
  depth: number;
  nodes: FlowNode[];
}

export interface FlowTurn {
  id: string;
  title: string;
  stages: FlowStage[];
  status: FlowStatus;
}

interface StableProgress {
  sessionId: string;
  turnId: string;
  completed: number;
  total: number;
  percent: number;
}

interface ChatOutlineItem {
  id: string;
  messageId: string;
  title: string;
  summary: string;
}

interface DagInfo {
  label: string;
  detail: string;
}

export interface SessionFileItem {
  key: string;
  title: string;
  path: string;
  source: "node" | "message";
  sourceLabel: string;
  summary?: string;
}

const statusLabel: Record<FlowStatus, string> = {
  pending: "待开始",
  running: "进行中",
  blocked: "阻塞",
  completed: "已完成",
  failed: "失败",
  cancelled: "已终止",
};

const statusHint: Record<FlowStatus, string> = {
  pending: "等待 Leader 分配或上游节点完成",
  running: "成员正在处理这个节点",
  blocked: "等待错误、权限、限流或补充信息处理",
  completed: "节点已产出结果，可进入下一步",
  failed: "节点执行失败，建议重试或改派",
  cancelled: "节点已终止",
};

const TEAM_PLAN_SOURCES = new Set(["team_plan", "team_kanban", "team_flow_fallback"]);
const dagStrategyLabel: Record<string, string> = {
  fast_minimal_path: "Fast DAG",
  standard_role_dag: "Standard Role DAG",
  standard_semantic_dag: "Standard Semantic DAG",
  heavy_single_dag: "AI Planner DAG",
  heavy_multi_candidate: "AI Planner DAG",
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
  lead: "Leader 拆分任务",
  plan: "任务规划",
  design: "方案设计",
  build: "成员执行任务",
  verify: "测试验证",
  release: "交付整理",
  docs: "文档整理",
  summary: "Leader 汇总结果",
  other: "协作节点",
};
const planNodeTitle: Record<string, string> = {
  leader_plan: "Leader 拆分任务",
  leader_review: "Leader 审阅结果",
  leader_summary: "Leader 汇总结果",
  fast_execute: "成员执行任务",
  fast_verify: "测试验证",
  build_design: "实现方案",
  test_plan: "测试方案",
  build: "开发实现",
  verify: "测试验证",
  docs: "文档整理",
  release: "交付整理",
};
function progressText(task: Task, key: string): string {
  return String(task.progress?.[key] || "").trim();
}

function progressNumber(task: Task, key: string): number | null {
  const value = task.progress?.[key];
  const number = typeof value === "number" ? value : Number(value);
  return Number.isFinite(number) ? number : null;
}

function progressStringList(task: Task, key: string): string[] {
  const value = task.progress?.[key];
  if (!Array.isArray(value)) return [];
  return value.map((item) => String(item || "").trim()).filter(Boolean);
}

function progressRecord(task: Task, key: string): Record<string, unknown> {
  const value = task.progress?.[key];
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function inferDagTier(strategy: string, tier: string): string {
  if (tier) return tier;
  if (strategy.startsWith("fast_")) return "fast";
  if (strategy.startsWith("heavy_")) return "ai";
  if (strategy.startsWith("standard_")) return "standard";
  return "";
}

export function teamDagInfo(nodes: FlowNode[], turnGroups: FlowTurn[]): DagInfo | null {
  if (nodes.length === 0) return null;
  const strategies = nodes
    .map((node) => progressText(node.raw, "plan_strategy"))
    .filter(Boolean);
  const strategy = strategies[0] || "";
  const tier = inferDagTier(strategy, nodes.map((node) => progressText(node.raw, "execution_tier")).find(Boolean) || "");
  const label = dagStrategyLabel[strategy] || (tier ? `${tier[0].toUpperCase()}${tier.slice(1)} DAG` : "Team DAG");
  const stageCount = turnGroups.reduce((count, turn) => count + turn.stages.length, 0);
  const detailParts = [
    strategy || tier,
    stageCount ? `${stageCount}层` : "",
    `${nodes.length}节点`,
  ].filter(Boolean);
  return { label, detail: detailParts.join(" · ") };
}

function contractSummaryItems(task: Task): string[] {
  const summaryItems = progressStringList(task, "summary_items");
  if (summaryItems.length > 0) return summaryItems;
  const contract = progressRecord(task, "result_contract");
  const items = [
    ["结论", contract.answer],
    ["依据", contract.evidence],
    ["风险", contract.risk],
    ["建议", contract.next_action],
  ].map(([label, value]) => {
    const text = String(value || "").trim();
    if (!text) return "";
    return text.startsWith(`${label}：`) || text.startsWith(`${label}:`) ? text : `${label}：${text}`;
  }).filter(Boolean);
  return items;
}

function normalizeStatus(task: Task): FlowStatus {
  const raw = String(task.status || "").toLowerCase();
  if (["done", "completed", "success"].includes(raw)) return "completed";
  if (["running", "in_progress"].includes(raw)) return "running";
  if (["pending", "ready", "queued"].includes(raw)) return "pending";
  if (["cancelled", "canceled"].includes(raw)) return "cancelled";
  if (["blocked", "waiting_input", "waiting"].includes(raw)) return "blocked";
  if (raw === "failed" || task.error) return "failed";
  return "pending";
}

function ownerOf(task: Task): string {
  const assignee = String(task.assignee || task.progress?.assignee || "").trim();
  if (assignee) return assignee;
  if (task.status === "blocked" && task.progress?.runtime_blocking) return "待分配";
  if (task.kind === "team") return "Team";
  if (task.kind === "subagent") return "Subagent";
  if (task.kind === "agent_turn") return "Leader";
  return String(task.kind || "Crew");
}

function cleanAgentName(value: unknown): string {
  const text = String(value || "").trim();
  if (!text) return "";
  const normalized = text
    .replace(/^crew::/i, "")
    .replace(/^agent[:：]\s*/i, "")
    .replace(/^member[:：]\s*/i, "")
    .trim();
  if (!normalized) return "";
  if (normalized === "general-purpose") return "subagent";
  return normalized.length > 18 ? `${normalized.slice(0, 17)}…` : normalized;
}

function pushAgent(list: string[], value: unknown): void {
  const agent = cleanAgentName(value);
  if (agent && !list.includes(agent)) list.push(agent);
}

function agentsOf(task: Task): string[] {
  const agents: string[] = [];
  const progress = task.progress || {};
  const kind = String(task.kind || "");
  const source = progressText(task, "source");
  const assignee = cleanAgentName(task.assignee || progress.assignee);

  if (kind === "team" && TEAM_PLAN_SOURCES.has(source)) {
    if (assignee.toLowerCase() === "leader") {
      pushAgent(agents, "Leader");
    } else {
      pushAgent(agents, assignee);
    }
    return agents;
  }
  if (kind === "team" && assignee && assignee.toLowerCase() !== "leader") pushAgent(agents, "Leader");
  if (kind === "agent_turn") pushAgent(agents, "Leader");
  pushAgent(agents, assignee);
  pushAgent(agents, progress.assignee);
  pushAgent(agents, progress.agent);
  pushAgent(agents, progress.member);
  pushAgent(agents, progress.requester_member_id);
  pushAgent(agents, progress.sender_member_id);

  if (agents.length === 0) pushAgent(agents, ownerOf(task));
  return agents.slice(0, 4);
}

function makeTeamMembers(nodes: FlowNode[]): TeamMemberView[] {
  const names: string[] = [];
  const roles = new Map<string, string>();
  for (const node of nodes) {
    for (const agent of node.agents) {
      if (!names.includes(agent)) names.push(agent);
      const label = progressText(node.raw, "role_label");
      if (label && !roles.has(agent)) roles.set(agent, label);
    }
  }
  if (names.length === 0) names.push("Leader");
  if (!names.includes("Leader")) names.unshift("Leader");
  return names.slice(0, 6).map((name) => ({ name, role: roles.get(name) || "按节点协议处理子任务" }));
}

export function nodeTitle(task: Task, index: number): string {
  const source = progressText(task, "source");
  if (String(task.kind || "") === "team" && TEAM_PLAN_SOURCES.has(source)) {
    const planId = String(task.progress?.plan_node_id || task.task_id || task.id || "").trim();
    const displayTitle = progressText(task, "display_title");
    if (displayTitle) return compactText(displayTitle, 24);
    const explicitTitle = nodeFullTitle(task);
    const structuralTitle = planId ? planNodeTitle[planId] : "";
    if (explicitTitle) {
      if (structuralTitle && explicitTitle.startsWith(`${structuralTitle}：`)) return structuralTitle;
      if (structuralTitle && explicitTitle.startsWith(`${structuralTitle}:`)) return structuralTitle;
      return shortNodeTitle(explicitTitle);
    }
    if (structuralTitle) return structuralTitle;
    const lane = progressText(task, "workflow_lane");
    if (lane && workflowLaneTitle[lane]) return workflowLaneTitle[lane];
    const roleLabel = progressText(task, "role_label");
    if (roleLabel) return `${roleLabel}节点`;
    return `协作节点 ${index + 1}`;
  }
  const title = String(task.title || "").trim();
  if (title) return title;
  const kind = String(task.kind || "任务");
  return `${kind} 节点 ${index + 1}`;
}

function nodeFullTitle(task: Task): string {
  return String(
    task.progress?.full_title
    || task.progress?.plan_node_title
    || task.progress?.title
    || task.title
    || "",
  ).trim();
}

function shortNodeTitle(title: string): string {
  const text = String(title || "")
    .replace(/\s+/g, " ")
    .trim();
  if (!text) return "";
  const firstClause = text.split(/[，,。；;：:（）()]/).map((item) => item.trim()).find(Boolean) || text;
  return compactText(firstClause, 18);
}

function taskTime(task: Task): number {
  for (const value of [task.started_at, task.created_at, task.last_activity_at, task.updated_at, task.finished_at]) {
    if (typeof value === "number" && Number.isFinite(value)) return value;
  }
  return Number.MAX_SAFE_INTEGER;
}

function displayRank(task: Task): number {
  const displayOrder = progressNumber(task, "display_order");
  if (displayOrder !== null) return displayOrder;
  return workflowLaneOrder[progressText(task, "workflow_lane")] ?? workflowLaneOrder.other;
}

function stringList(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.map((item) => String(item || "").trim()).filter(Boolean);
}

function artifactPaths(task: Task): string[] {
  const fromProgress = stringList(task.progress?.artifact_paths);
  const fromOutput = String(task.output_ref || "")
    .split(/\r?\n/)
    .map((item) => item.trim())
    .filter(Boolean)
    .filter((path) => !/(?:^|[\\/])tasks[\\/][^\\/]+\.json$/i.test(path));
  return Array.from(new Set([...fromProgress, ...fromOutput]));
}

function fileNameOf(path: string): string {
  const text = String(path || "").trim();
  if (!text) return "产物";
  return text.split(/[\\/]/).filter(Boolean).pop() || text;
}

function pushSessionFile(
  items: SessionFileItem[],
  seen: Set<string>,
  item: Omit<SessionFileItem, "key">,
): void {
  const path = String(item.path || "").trim();
  if (!path) return;
  const key = path.toLowerCase();
  if (seen.has(key)) return;
  seen.add(key);
  items.push({
    ...item,
    path,
    key: path,
    title: item.title || fileNameOf(path),
  });
}

export function collectSessionFileItems(tasks: Task[], messages: UiMessage[]): SessionFileItem[] {
  const items: SessionFileItem[] = [];
  const seen = new Set<string>();
  for (const task of tasks) {
    const sourceLabel = progressText(task, "title")
      || progressText(task, "plan_node_title")
      || plainTaskText(task.title || task.id || "节点产物").slice(0, 28);
    for (const path of artifactPaths(task)) {
      pushSessionFile(items, seen, {
        title: fileNameOf(path),
        path,
        source: "node",
        sourceLabel,
      });
    }
  }
  for (const message of messages) {
    for (const artifact of message.artifacts || []) {
      const path = String(artifact.path || "").trim();
      if (!path) continue;
      pushSessionFile(items, seen, {
        title: artifact.title || fileNameOf(path),
        path,
        source: "message",
        sourceLabel: message.agentName || (message.isLeader ? "Leader" : "Team 消息"),
        summary: artifact.summary || artifact.mime_type || artifact.content_type || artifact.kind || "",
      });
    }
  }
  return items;
}

function durationLabel(task: Task): string {
  const start = typeof task.started_at === "number" && Number.isFinite(task.started_at)
    ? task.started_at
    : typeof task.created_at === "number" && Number.isFinite(task.created_at)
      ? task.created_at
      : 0;
  const end = typeof task.finished_at === "number" && Number.isFinite(task.finished_at)
    ? task.finished_at
    : typeof task.updated_at === "number" && Number.isFinite(task.updated_at)
      ? task.updated_at
      : 0;
  if (!start || !end || end < start) return "";
  const seconds = Math.max(0, Math.round(end - start));
  if (seconds < 1) return "<1s";
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return rest ? `${minutes}m ${rest}s` : `${minutes}m`;
}

function planNodeId(node: FlowNode): string {
  return String(node.raw.progress?.plan_node_id || node.id || "").trim();
}

function parentNodeIds(node: FlowNode): string[] {
  return stringList(node.raw.progress?.parent_node_ids);
}

function hasTeamPlanNodes(nodes: FlowNode[]): boolean {
  return nodes.some((node) => {
    const source = progressText(node.raw, "source");
    return TEAM_PLAN_SOURCES.has(source);
  });
}

function makeDagStages(nodes: FlowNode[]): FlowStage[] {
  const ordered = sortTasksForFlow(nodes.map((node) => node.raw))
    .map((task) => nodes.find((node) => node.raw === task))
    .filter((node): node is FlowNode => Boolean(node));
  if (!hasTeamPlanNodes(ordered)) {
    return ordered.map((node, index) => ({ id: `stage_${index}`, depth: index, nodes: [node] }));
  }

  const byPlanNodeId = new Map<string, FlowNode>();
  const byDependencyId = new Map<string, FlowNode>();
  for (const node of ordered) {
    const nodeId = planNodeId(node);
    if (nodeId) byPlanNodeId.set(nodeId, node);
    for (const id of [nodeId, node.raw.task_id, node.raw.id]) {
      const value = String(id || "").trim();
      if (value) byDependencyId.set(value, node);
    }
  }

  const depthMemo = new Map<string, number>();
  const visiting = new Set<string>();
  const depthOf = (node: FlowNode): number => {
    const nodeId = planNodeId(node);
    if (!nodeId) return 0;
    const cached = depthMemo.get(nodeId);
    if (cached !== undefined) return cached;
    if (visiting.has(nodeId)) return 0;
    visiting.add(nodeId);
    const parents = parentNodeIds(node)
      .map((parentId) => byDependencyId.get(parentId) || byPlanNodeId.get(parentId))
      .filter((parent): parent is FlowNode => Boolean(parent));
    const depth = parents.length ? Math.max(...parents.map(depthOf)) + 1 : 0;
    visiting.delete(nodeId);
    depthMemo.set(nodeId, depth);
    return depth;
  };

  const groups = new Map<number, FlowNode[]>();
  for (const node of ordered) {
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

function turnIdOf(node: FlowNode): string {
  return String(node.raw.progress?.turn_session_id || node.raw.session_id || "team_turn").trim();
}

function turnTitleOf(nodes: FlowNode[], index: number): string {
  const first = nodes[0];
  const rawTitle = String(first?.raw.progress?.turn_title || first?.raw.detail || first?.title || "").trim();
  if (rawTitle) return compactText(rawTitle, 42);
  return `团队任务 ${index + 1}`;
}

export function makeTurnGroups(nodes: FlowNode[]): FlowTurn[] {
  const ordered = sortTasksForFlow(nodes.map((node) => node.raw))
    .map((task) => nodes.find((node) => node.raw === task))
    .filter((node): node is FlowNode => Boolean(node));
  const groups = new Map<string, FlowNode[]>();
  for (const node of ordered) {
    const turnId = turnIdOf(node);
    groups.set(turnId, [...(groups.get(turnId) || []), node]);
  }
  return [...groups.entries()].map(([id, groupNodes], index) => {
    const stages = makeDagStages(groupNodes);
    const syntheticStage: FlowStage = {
      id: `${id}:prompt`,
      depth: -1,
      nodes: groupNodes,
    };
    return {
      id,
      title: turnTitleOf(groupNodes, index),
      stages,
      status: stageStatus(syntheticStage),
    };
  });
}

function nodesForTurn(turn: FlowTurn | null | undefined): FlowNode[] {
  if (!turn) return [];
  const seen = new Set<string>();
  const nodes: FlowNode[] = [];
  for (const stage of turn.stages) {
    for (const node of stage.nodes) {
      if (seen.has(node.id)) continue;
      seen.add(node.id);
      nodes.push(node);
    }
  }
  return nodes;
}

export function currentTurnNodesForBoard(nodes: FlowNode[], turnGroups: FlowTurn[]): FlowNode[] {
  const latestNodes = nodesForTurn(turnGroups[turnGroups.length - 1] || null);
  return latestNodes.length > 0 ? latestNodes : nodes;
}

function stageStatus(stage: FlowStage): FlowStatus {
  if (stage.nodes.some((node) => node.status === "running")) return "running";
  if (stage.nodes.some((node) => node.status === "failed")) return "failed";
  if (stage.nodes.some((node) => node.status === "blocked")) return "blocked";
  if (stage.nodes.every((node) => node.status === "completed")) return "completed";
  if (stage.nodes.every((node) => node.status === "cancelled")) return "cancelled";
  return "pending";
}

function sortTasksForFlow(tasks: Task[]): Task[] {
  return [...tasks].sort((a, b) => {
    const at = taskTime(a);
    const bt = taskTime(b);
    if (at !== bt) return at - bt;
    const ar = displayRank(a);
    const br = displayRank(b);
    if (ar !== br) return ar - br;
    return String(a.id || a.task_id || "").localeCompare(String(b.id || b.task_id || ""));
  });
}

function readableSummary(task: Task): string {
  const contractItems = contractSummaryItems(task);
  if (contractItems.length > 0) return compactTaskSummary(contractItems.join("；"));
  const error = String(task.error || "").trim();
  if (error) {
    const errorKind = progressText(task, "error_kind");
    if (errorKind === "rate_limit") {
      return "模型请求触发限流，可稍后重试或降低并发。";
    }
    if (errorKind === "delegate_tool_unavailable") {
      return "派活工具不可用：请确认当前会话是 Team 模式，并由 Crew 内部 Leader 发起。";
    }
    return compactTaskSummary(error);
  }
  const result = String(task.result || "").trim();
  if (result) return compactTaskSummary(result);
  const progress = task.progress || {};
  const text = String(progress.last_chunk || progress.output_tail || progress.last_tool || "").trim();
  if (text) return compactTaskSummary(text);
  const status = normalizeStatus(task);
  return statusHint[status];
}

function readableSummaryItems(task: Task): string[] {
  const contractItems = contractSummaryItems(task);
  if (contractItems.length > 0) return contractItems;
  const error = String(task.error || "").trim();
  if (error) {
    const errorKind = progressText(task, "error_kind");
    if (errorKind === "rate_limit") {
      return ["模型请求触发限流，可稍后重试或降低并发。"];
    }
    if (errorKind === "delegate_tool_unavailable") {
      return ["派活工具不可用：请确认当前会话是 Team 模式，并由 Crew 内部 Leader 发起。"];
    }
    return compactTaskSummaryItems(error);
  }
  const result = String(task.result || "").trim();
  if (result) return compactTaskSummaryItems(result);
  const progress = task.progress || {};
  const text = String(progress.last_chunk || progress.output_tail || progress.last_tool || "").trim();
  if (text) return compactTaskSummaryItems(text);
  const status = normalizeStatus(task);
  return [statusHint[status]];
}

export function makeNodes(tasks: Task[]): FlowNode[] {
  return sortTasksForFlow(tasks).map((task, index) => ({
    id: task.task_id || task.id || `node_${index}`,
    title: nodeTitle(task, index),
    fullTitle: nodeFullTitle(task),
    owner: ownerOf(task),
    agents: agentsOf(task),
    status: normalizeStatus(task),
    summary: readableSummary(task),
    summaryItems: readableSummaryItems(task),
    raw: task,
  }));
}

function dependencyLabel(node: FlowNode, nodes: FlowNode[]): string {
  const parents = parentNodeIds(node);
  if (parents.length === 0) return "";
  const byId = new Map<string, string>();
  for (const item of nodes) {
    for (const id of [planNodeId(item), item.raw.task_id, item.raw.id]) {
      const value = String(id || "").trim();
      if (value) byId.set(value, item.title);
    }
  }
  return parents.map((parentId) => compactText(byId.get(parentId) || parentId, 18)).join("、");
}

function compactText(text: string, max = 90): string {
  return compactTaskText(text, max);
}

export function nodeMessageId(node: FlowNode, messages: UiMessage[]): string {
  const nodeIds = new Set(
    [planNodeId(node), node.raw.task_id, node.raw.id]
      .map((value) => String(value || "").trim())
      .filter(Boolean),
  );
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (message.nodeId && nodeIds.has(String(message.nodeId).trim())) return message.id;
  }
  return "";
}

function makeChatOutline(messages: UiMessage[]): ChatOutlineItem[] {
  return messages
    .filter((message) => message.role === "user" && compactText(message.text))
    .slice(-10)
    .map((message, index) => {
      return {
        id: message.id || `outline_${index}`,
        messageId: message.id,
        title: `用户消息 ${index + 1}`,
        summary: compactText(message.text, 72),
      };
    });
}

function sessionLogoText(label?: Session["agent_label"]): string {
  const provider = label?.provider?.trim().toLowerCase() || "crew";
  if (provider === "crew") return "Crew";
  return label?.display_badge?.trim() || "?";
}

function sessionSummary(messages: UiMessage[]): string {
  const lastAssistant = [...messages].reverse().find((message) => message.role === "assistant" && compactText(message.text));
  if (lastAssistant) return compactText(lastAssistant.text, 140);
  const lastUser = [...messages].reverse().find((message) => message.role === "user" && compactText(message.text));
  if (lastUser) return `用户需求：${compactText(lastUser.text, 120)}`;
  return "当前会话还没有可总结的聊天内容。";
}

function toneFor(agent: string, tones: Record<string, number>): number {
  return tones[agent] ?? 0;
}

export default function TaskBoard({
  sessionId,
  tasks,
  mode,
  messages,
  currentAgentLabel,
  teamMembers: configuredTeamMembers,
  boardWidth,
  onBoardWidthChange,
  onJumpToMessage,
  onClose,
  onCancel,
  onRecover,
}: Props) {
  const [runtime, setRuntime] = useState<RuntimeConcurrency | null>(null);
  const [expandedNodes, setExpandedNodes] = useState<Set<string>>(new Set());
  const [expandedTurns, setExpandedTurns] = useState<Set<string>>(new Set());
  const [recoveryAssignees, setRecoveryAssignees] = useState<Record<string, string>>({});
  const [filesOpen, setFilesOpen] = useState(false);
  const [, setKnownTurnIds] = useState<Set<string>>(new Set());
  const [stableProgress, setStableProgress] = useState<StableProgress>({
    sessionId: "",
    turnId: "",
    completed: 0,
    total: 0,
    percent: 0,
  });
  const [stableTeamNodes, setStableTeamNodes] = useState<{ sessionId: string; nodes: FlowNode[] }>({
    sessionId: "",
    nodes: [],
  });
  const isTeamMode = mode === "team";
  const availableTeamMembers = configuredTeamMembers ?? [];

  useEffect(() => {
    setFilesOpen(false);
  }, [isTeamMode, sessionId]);

  useEffect(() => {
    let disposed = false;
    const load = async () => {
      try {
        const data = await api.runtimeConcurrency();
        if (!disposed) setRuntime(data);
      } catch {
        if (!disposed) setRuntime(null);
      }
    };
    load();
    const timer = window.setInterval(load, 2000);
    return () => {
      disposed = true;
      window.clearInterval(timer);
    };
  }, []);

  const activeSessions = useMemo(
    () => Object.values(runtime?.sessions ?? {})
      .filter((s) => s.live !== "idle" || s.queue_depth > 0),
    [runtime],
  );
  const activeChildCount = useMemo(() => {
    const snapshot = runtime?.active_children;
    if (!snapshot) return 0;
    if (Array.isArray(snapshot)) return snapshot.length;
    return Object.values(snapshot).reduce((n, children) => n + children.length, 0);
  }, [runtime]);
  const nodes = useMemo(() => makeNodes(tasks), [tasks]);
  const startResize = (event: ReactPointerEvent<HTMLButtonElement>) => {
    event.preventDefault();
    const startX = event.clientX;
    const startWidth = boardWidth;
    const move = (moveEvent: PointerEvent) => {
      const next = Math.max(300, Math.min(680, Math.round(startWidth + startX - moveEvent.clientX)));
      onBoardWidthChange(next);
    };
    const stop = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", stop);
      document.body.classList.remove("is-resizing-board");
    };
    document.body.classList.add("is-resizing-board");
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", stop, { once: true });
  };
  useEffect(() => {
    if (!isTeamMode) {
      setStableTeamNodes({ sessionId, nodes: [] });
      return;
    }
    setStableTeamNodes((prev) => {
      if (prev.sessionId !== sessionId) return { sessionId, nodes };
      if (nodes.length === 0) return prev;
      return { sessionId, nodes };
    });
  }, [isTeamMode, nodes, sessionId]);
  const displayNodes = isTeamMode
    && nodes.length === 0
    && stableTeamNodes.sessionId === sessionId
    && stableTeamNodes.nodes.length > 0
    ? stableTeamNodes.nodes
    : nodes;
  const turnGroups = useMemo(() => makeTurnGroups(displayNodes), [displayNodes]);
  const currentTurn = turnGroups[turnGroups.length - 1] || null;
  const currentTurnNodes = useMemo(
    () => currentTurnNodesForBoard(displayNodes, turnGroups),
    [displayNodes, turnGroups],
  );
  const currentTurnGroups = currentTurn ? [currentTurn] : turnGroups;
  const dagInfo = useMemo(
    () => teamDagInfo(currentTurnNodes, currentTurnGroups),
    [currentTurnGroups, currentTurnNodes],
  );
  useEffect(() => {
    if (!isTeamMode) {
      setExpandedTurns(new Set());
      setKnownTurnIds(new Set());
      return;
    }
    const currentIds = new Set(turnGroups.map((turn) => turn.id));
    setKnownTurnIds((known) => {
      setExpandedTurns((prev) => {
        const next = new Set(prev);
        for (const id of currentIds) {
          if (!known.has(id)) next.add(id);
        }
        for (const id of next) {
          if (!currentIds.has(id)) next.delete(id);
        }
        return next;
      });
      return currentIds;
    });
  }, [isTeamMode, turnGroups]);
  const inferredTeamMembers = useMemo(() => makeTeamMembers(displayNodes), [displayNodes]);
  const teamMembers = useMemo(
    () => (configuredTeamMembers && configuredTeamMembers.length > 0 ? configuredTeamMembers : inferredTeamMembers),
    [configuredTeamMembers, inferredTeamMembers],
  );
  const leaderDisplayName = useMemo(
    () => teamMembers.find((member) => member.isLeader)?.name || "Leader",
    [teamMembers],
  );
  const displayAgentName = (agent: string) => {
    const text = String(agent || "").trim();
    return text.toLowerCase() === "leader" ? leaderDisplayName : text;
  };
  const memberTones = useMemo(
    () => Object.fromEntries(teamMembers.map((member, index) => [member.name, member.tone ?? index % 6])),
    [teamMembers],
  );
  const completedCount = currentTurnNodes.filter((node) => node.status === "completed").length;
  const runningNode = currentTurnNodes.find((node) => node.status === "running");
  const errorNodes = currentTurnNodes.filter((node) => String(node.raw.error || "").trim());
  const progressUnits = completedCount + (runningNode ? 0.35 : 0);
  const rawProgress = currentTurnNodes.length ? Math.min(100, Math.round((progressUnits / currentTurnNodes.length) * 100)) : 0;
  const toggleTurn = (turnId: string) => {
    setExpandedTurns((prev) => {
      const next = new Set(prev);
      if (next.has(turnId)) next.delete(turnId);
      else next.add(turnId);
      return next;
    });
  };
  useEffect(() => {
    if (!isTeamMode) {
      setStableProgress({ sessionId, turnId: "", completed: 0, total: 0, percent: 0 });
      return;
    }
    const turnId = currentTurn?.id || "";
    setStableProgress((prev) => {
      if (prev.sessionId !== sessionId || prev.turnId !== turnId) {
        return { sessionId, turnId, completed: completedCount, total: currentTurnNodes.length, percent: rawProgress };
      }
      if (currentTurnNodes.length === 0) return prev;
      return {
        sessionId,
        turnId,
        completed: Math.max(prev.completed, completedCount),
        total: Math.max(prev.total, currentTurnNodes.length),
        percent: Math.max(prev.percent, rawProgress),
      };
    });
  }, [completedCount, currentTurn?.id, currentTurnNodes.length, isTeamMode, rawProgress, sessionId]);
  const stableProgressMatches = stableProgress.sessionId === sessionId
    && stableProgress.turnId === (currentTurn?.id || "");
  const displayCompletedCount = stableProgressMatches ? stableProgress.completed : completedCount;
  const displayTotalCount = stableProgressMatches ? stableProgress.total : currentTurnNodes.length;
  const progress = stableProgressMatches ? stableProgress.percent : rawProgress;
  const nextNode = currentTurnNodes.find((node) => node.status === "running")
    || currentTurnNodes.find((node) => node.status === "pending")
    || currentTurnNodes[currentTurnNodes.length - 1];
  const displayRunningNode = currentTurnNodes.find((node) => node.status === "running");
  const allTeamNodesCompleted = currentTurnNodes.length > 0 && currentTurnNodes.every((node) => node.status === "completed");
  const chatOutline = useMemo(() => makeChatOutline(messages), [messages]);
  const chatSummary = useMemo(() => sessionSummary(messages), [messages]);
  const sessionFiles = useMemo(() => collectSessionFileItems(displayNodes.map((node) => node.raw), messages), [displayNodes, messages]);
  const toggleNode = (nodeId: string) => {
    setExpandedNodes((prev) => {
      const next = new Set(prev);
      next.has(nodeId) ? next.delete(nodeId) : next.add(nodeId);
      return next;
    });
  };

  return (
    <aside className="board">
      <button
        className="board-resizer"
        type="button"
        onPointerDown={startResize}
        title="拖动调整看板宽度"
        aria-label="拖动调整看板宽度"
      />
      <div className="board__head">
        <div>
          <span className="board__title-row">
            {isTeamMode ? "协作看板" : "执行看板"}
            {isTeamMode && (
              <button
                className={"board-files-btn" + (filesOpen ? " is-active" : "")}
                type="button"
                onClick={() => setFilesOpen((value) => !value)}
                title="查看当前 session 文件清单"
                aria-label="查看当前 session 文件清单"
                aria-expanded={filesOpen}
              >
                <img src={artifactsFolderIcon} alt="" aria-hidden="true" />
                {sessionFiles.length > 0 && <i>{sessionFiles.length}</i>}
              </button>
            )}
          </span>
          <em>{isTeamMode ? "Team Flow" : "Execution Flow"}</em>
        </div>
        <div className="board__actions">
          <button className="icon-btn" style={{ width: 24, height: 24 }} onClick={onClose} title="收起">
            ×
          </button>
        </div>
      </div>
      {isTeamMode && filesOpen && (
        <section className="board-files-panel" aria-label="当前 session 文件清单">
          <div className="board-files-panel__head">
            <strong>产物文件</strong>
            <span>{sessionFiles.length ? `${sessionFiles.length} 个` : "暂无"}</span>
          </div>
          {sessionFiles.length === 0 ? (
            <p className="board-files-panel__empty">团队产出文件后，会在这里汇总成当前 session 的文件清单。</p>
          ) : (
            <div className="board-files-panel__list">
              {sessionFiles.map((file) => (
                <button
                  type="button"
                  className="board-file-item"
                  key={file.key}
                  title={file.path}
                  onClick={() => void openLocalPath(file.path)}
                >
                  <i className="flow-node__file-icon" aria-hidden="true" />
                  <span>
                    <strong>{file.title}</strong>
                    <em>{file.sourceLabel}{file.summary ? ` · ${file.summary}` : ""}</em>
                  </span>
                </button>
              ))}
            </div>
          )}
        </section>
      )}
      <div className="board__list">
        {!isTeamMode ? (
          <>
            <section className="flow-hero chat-outline-hero">
              <div className="flow-hero__title">
                <span
                  className="session__agent-badge board-session-badge"
                  aria-hidden="true"
                >
                  {sessionLogoText(currentAgentLabel)}
                </span>
                <div className="session-summary">
                  <strong>会话总结</strong>
                  <p className="session-summary__box">{chatSummary}</p>
                </div>
              </div>
            </section>

            {chatOutline.length === 0 ? (
              <div className="board__empty board__empty--cute">
                <span className="pixel-empty" aria-hidden="true" />
                <strong>还没有聊天大纲</strong>
                <p>开始对话后，这里会整理用户消息的概括版。</p>
              </div>
            ) : (
              <section className="chat-outline" aria-label="用户消息大纲">
                {chatOutline.map((item) => (
                  <article className="chat-outline__item is-user" key={item.id}>
                    <span className="chat-outline__dot" aria-hidden="true" />
                    <div>
                      <button
                        className="chat-outline__button"
                        onClick={() => onJumpToMessage?.(item.messageId)}
                      >
                        <span>{item.title}</span>
                        <em>定位</em>
                      </button>
                      <p>{item.summary}</p>
                    </div>
                  </article>
                ))}
              </section>
            )}
          </>
        ) : (
          <section className="flow-hero">
          <div className="flow-hero__title">
            <span className="team-mark team-mark--hero" aria-hidden="true">
              <i />
              <i />
            </span>
            <div>
              <strong>{displayNodes.length ? "团队工作流" : "等待团队工作流"}</strong>
              <p>
                {nextNode
                  ? allTeamNodesCompleted ? `已完成：${nextNode.title}` : `当前节点：${nextNode.title}`
                  : "开始任务后这里会展示 DAG 阶段、负责人和节点小结。"}
              </p>
            </div>
          </div>
          <div className="flow-meter" aria-label={`完成进度 ${progress}%`}>
            <span style={{ width: `${progress}%` }} />
          </div>
          <div className="flow-hero__meta">
            <span>{displayCompletedCount}/{displayTotalCount || 0} 已完成</span>
            <span>{displayRunningNode ? `${displayAgentName(displayRunningNode.owner)} 处理中` : "暂无运行节点"}</span>
          </div>
          <div className="team-members" aria-label="团队成员">
            {teamMembers.map((member) => (
              <div className={`team-member${member.isLeader ? " is-leader" : ""}`} key={member.name}>
                {member.agentId === CREW_BUILTIN_AGENT_ID ? (
                  <span className="team-member__crew-avatar msg__avatar bot" aria-hidden="true">
                    <AgentAvatarLogo />
                  </span>
                ) : (
                  <span className={`agent-avatar agent-tone-${toneFor(member.name, memberTones)}`}>
                    {member.displayBadge || "?"}
                  </span>
                )}
                <div>
                  <strong>
                    {member.name}
                    {member.isLeader && <span className="pixel-flag" aria-label="Leader" title="Leader" />}
                  </strong>
                  <p>{member.role}</p>
                </div>
              </div>
            ))}
          </div>
        </section>
        )}

        {isTeamMode && displayNodes.length === 0 ? (
          <div className="board__empty board__empty--cute">
            <span className="pixel-empty" aria-hidden="true" />
            <strong>还没有流程节点</strong>
            <p>点击团队派活或开始 Team 会话后，这里会按节点展示负责人、进度和小结。</p>
          </div>
        ) : isTeamMode && (
          <section className="flow-map" aria-label={isTeamMode ? "团队运行流程" : "任务执行流程"}>
            {turnGroups.map((turn, turnIndex) => (
              <section className={`flow-turn is-${turn.status}`} key={turn.id}>
                <button
                  className="flow-turn__head"
                  onClick={() => toggleTurn(turn.id)}
                  aria-expanded={expandedTurns.has(turn.id)}
                >
                  <span>{turnIndex + 1}</span>
                  <div>
                    <strong>{turn.title}</strong>
                    <em>{statusLabel[turn.status]}</em>
                  </div>
                </button>
                {expandedTurns.has(turn.id) && turn.stages.map((stage) => (
                  <div className={`flow-stage is-${stageStatus(stage)}`} key={stage.id}>
                    <div className="flow-stage__rail">
                      <span>{stage.depth + 1}</span>
                      <i />
                    </div>
                    <div className="flow-stage__nodes">
                      {stage.nodes.map((node) => {
                    const dependency = dependencyLabel(node, displayNodes);
                    return (
                      <article className={`flow-node is-${node.status}`} key={node.id}>
                        <div className="flow-node__card">
                          <button
                            className="flow-node__summary-btn"
                            onClick={() => toggleNode(node.id)}
                            aria-expanded={expandedNodes.has(node.id)}
                          >
                            <div className="flow-node__top">
                              <span className="agent-chip">主责：{displayAgentName(node.owner)}</span>
                              <span className={`flow-status is-${node.status}`}>{statusLabel[node.status]}</span>
                            </div>
                            <strong className="flow-node__title" title={node.fullTitle || node.title}>{node.title}</strong>
                            {dependency && <span className="flow-node__dependency">依赖：{dependency}</span>}
                            <span className="flow-node__hint">{expandedNodes.has(node.id) ? "收起详情" : "点击展开详情"}</span>
                          </button>
                          {expandedNodes.has(node.id) && (
                            <div className="flow-node__detail">
                              {(() => {
                                const paths = artifactPaths(node.raw);
                                const duration = durationLabel(node.raw);
                                const messageId = nodeMessageId(node, messages);
                                return (
                                  <>
                                    <section className="flow-node__brief" aria-label="节点摘要">
                                      <div className="flow-node__meta-line">
                                        <span>负责人 {displayAgentName(node.owner)}</span>
                                        {node.owner === "待分配" && String(node.raw.progress?.previous_assignee || "").trim() && (
                                          <span>原主责 {displayAgentName(String(node.raw.progress?.previous_assignee || ""))}</span>
                                        )}
                                        <span>{statusLabel[node.status]}</span>
                                        {duration && <span>{duration}</span>}
                                      </div>
                                      {node.fullTitle && node.fullTitle !== node.title && (
                                        <p className="flow-node__full-title">节点任务：{node.fullTitle}</p>
                                      )}
                                      <ul className="flow-node__brief-list">
                                        {(node.summaryItems.length > 0 ? node.summaryItems : [node.summary]).map((item, index) => (
                                          <li key={`${node.id}_summary_${index}`}>{item}</li>
                                        ))}
                                      </ul>
                                    </section>

                                    {paths.length > 0 && (
                                      <section className="flow-node__artifacts" aria-label="节点产物">
                                        <span>产物</span>
                                        {paths.map((path) => (
                                          <button type="button" onClick={() => void openLocalPath(path)} title={path} key={path}>
                                            <i className="flow-node__file-icon" aria-hidden="true" />
                                            <strong>{fileNameOf(path)}</strong>
                                          </button>
                                        ))}
                                      </section>
                                    )}

                                    {messageId && onJumpToMessage && (
                                      <div className="flow-node__actions flow-node__locate-actions">
                                        <button
                                          className="mini-cancel flow-node__locate"
                                          type="button"
                                          onClick={() => onJumpToMessage(messageId)}
                                        >
                                          定位
                                        </button>
                                      </div>
                                    )}
                                  </>
                                );
                              })()}
                              {node.status === "blocked" && (
                                <div className="flow-node__actions flow-node__recovery-actions">
                                  <span>阻塞节点处理</span>
                                  {String(node.raw.progress?.previous_assignee || "").trim() && (
                                    <button
                                      className="mini-cancel"
                                      type="button"
                                      onClick={() => void onRecover(
                                        node.id,
                                        "retry",
                                        String(node.raw.progress?.previous_assignee || "").trim(),
                                      )}
                                    >
                                      重试原成员
                                    </button>
                                  )}
                                  {availableTeamMembers.filter((member) => !member.isLeader && member.name).length > 0 && (
                                    <>
                                      <select
                                        className="mini-recovery-select"
                                        aria-label="选择恢复成员"
                                        value={recoveryAssignees[node.id] || availableTeamMembers.find((member) => !member.isLeader && member.name)?.name || ""}
                                        onChange={(event) => setRecoveryAssignees((current) => ({
                                          ...current,
                                          [node.id]: event.target.value,
                                        }))}
                                      >
                                        {availableTeamMembers
                                          .filter((member) => !member.isLeader && member.name)
                                          .map((member) => <option key={member.name} value={member.name}>{member.name}</option>)}
                                      </select>
                                      <button
                                        className="mini-cancel"
                                        type="button"
                                        onClick={() => void onRecover(
                                          node.id,
                                          "reassign",
                                          recoveryAssignees[node.id] || availableTeamMembers.find((member) => !member.isLeader && member.name)?.name || "",
                                        )}
                                      >
                                        重新分配
                                      </button>
                                    </>
                                  )}
                                  <button
                                    className="mini-cancel"
                                    type="button"
                                    onClick={() => void onRecover(node.id, "abandon")}
                                  >
                                    放弃节点
                                  </button>
                                </div>
                              )}
                              {["pending", "running"].includes(node.raw.status) && (
                                <div className="flow-node__actions">
                                  <button className="mini-cancel" onClick={() => void onCancel(node.raw.task_id || node.raw.id)}>
                                    取消节点
                                  </button>
                                </div>
                              )}
                            </div>
                          )}
                        </div>
                      </article>
                    );
                      })}
                    </div>
                  </div>
                ))}
              </section>
            ))}
          </section>
        )}

        {isTeamMode && errorNodes.length > 0 && (
          <section className="board-alert">
            <strong>错误待处理</strong>
            <p>{errorNodes[0].title}：{errorNodes[0].summary}</p>
          </section>
        )}

        <section className="runtime-box">
          <div className="runtime-box__head">
            <span>运行态</span>
            <span>{runtime ? `${runtime.global_active}/${runtime.max_active_runs || "∞"}` : "—"}</span>
          </div>
          {isTeamMode && (
            <div className="runtime-box__current">
              <span title={sessionId}>{sessionId}</span>
              <em title={dagInfo?.detail || "等待 DAG"}>{dagInfo ? `${dagInfo.label} · ${dagInfo.detail}` : "等待 DAG"}</em>
            </div>
          )}
          <div className="runtime-box__grid">
            <div>
              <span>全局排队</span>
              <b>{runtime?.global_queued ?? 0}</b>
            </div>
            <div>
              <span>活跃子任务</span>
              <b>{activeChildCount}</b>
            </div>
          </div>
          {activeSessions.length > 0 ? (
            <div className="runtime-box__sessions">
              {activeSessions.slice(0, 4).map((s) => (
                <div className="runtime-session" key={s.session_id}>
                  <span title={s.session_id}>{s.session_id}</span>
                  <em>{s.live}{s.queue_depth ? ` · q${s.queue_depth}` : ""}</em>
                </div>
              ))}
            </div>
          ) : (
            <div className="runtime-box__empty">暂无运行或排队会话</div>
          )}
        </section>
      </div>
    </aside>
  );
}
