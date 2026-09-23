/**
 * 会话/Agent 客户端：会话生命周期、Agent 配置、外部 Agent/Team、运行时、
 * 浏览器、Cron、Task、Dynamic Kanban、追问，以及 BackendChatSocket。
 */
import { logStream } from '../stream-debug';
import { reportRendererError } from '../renderer-error-report';
import {
  getJSON,
  jsonBody,
  gatewayFetch,
  gatewayBase,
  CrewBridge,
  wsBase,
  readDynamicKanbanResumeStream,
} from './transport';
import type { SendPayload, ChatChunk, BackendSocketStatusMeta } from './transport';

// ── 会话 / Agent 配置 ──

/** 写入 /api/session/{id}/agent-config 的 payload；外援 Agent id 只放在 external 下。 */
export interface SessionAgentConfig {
  executor: 'builtin' | 'client' | 'external' | 'acp' | 'team';
  capability_profiles?: string[];
  external?: { external_agent_id?: string; model?: string; [k: string]: unknown };
  acp?: { external_agent_id?: string; [k: string]: unknown };
  team?: { external_team_id?: string; [k: string]: unknown };
  [k: string]: unknown;
}

export type SessionAgentBindingKind =
  | 'builtin'
  | 'client'
  | 'external_agent'
  | 'external_team';

export interface SessionAgentBinding {
  kind: SessionAgentBindingKind;
  id: string;
}

export interface BackendSession {
  session_id: string;
  title: string;
  message_count: number;
  updated_at: number;
  created_at?: number;
  workspace_id: string;
  /** 后端 2026-06-29 起新增，旧后端可能不返回，缺省视为 false。 */
  archived?: boolean;
  pinned?: boolean;
  model_profile_id?: string;
  pending_model_profile_id?: string | null;
  model_label?: string;
  agent_label?: { name?: string; provider?: string; display_badge?: string; model?: string };
  /** 后端判定的会话执行身份；外援判断只依赖该字段。 */
  agent_binding?: SessionAgentBinding;
}

export interface BackendHistoryFileChange {
  path: string;
  name: string;
  added: number;
  removed: number;
  status: string;
  binary?: boolean;
}

export interface BackendHistoryItem {
  role: string;
  content: string;
  timestamp?: number;
  turn_started_at?: number;
  turn_duration?: number;
  thinking?: string;
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
  artifacts?: TeamArtifactCard[];
  /** 实际生成本条 assistant 消息的模型；旧历史可能缺失。 */
  model?: string;
  /** 本轮文件改动摘要（历史回放「已编辑文件」卡）；旧会话可能缺失。 */
  turn_file_changes?: BackendHistoryFileChange[];
  tool_calls?: Array<{
    id: string;
    name: string;
    ui_label?: string;
    arguments: Record<string, unknown>;
    result?: string;
    status?: string;
    started_at?: number;
    duration?: number;
  }>;
}

export interface TeamArtifactCard {
  id?: string;
  artifact_id?: string;
  title?: string;
  summary?: string;
  path?: string;
  content_type?: string;
  mime_type?: string;
  kind?: string;
}

export interface SessionPlanState {
  session_id: string;
  active: boolean;
  awaiting_approval: boolean;
  phase?: string;
  status?: string;
  has_plan: boolean;
  plan: string;
  plan_file: string;
  options?: { label: string; description: string }[];
}

// ── 外部运行时 / Agent / Team ──

export interface ExternalRuntime {
  id: string;
  name: string;
  provider: string;
  display_badge?: string;
  protocol?: string;
  path?: string;
  executable_path?: string;
  version?: string;
  detected_at?: number;
  healthy?: boolean;
  available?: boolean;
  availability_status?: 'ready' | 'degraded' | 'unavailable';
  metadata?: Record<string, unknown>;
}

export interface RuntimeModelProfile {
  id: string;
  label: string;
  provider?: string;
  default?: boolean;
  loaded?: boolean;
  has_key?: boolean;
  capabilities?: string[];
  thinking_levels?: string[];
}

export interface TeamMemberModelBinding {
  member_id: string;
  member_name: string;
  is_leader: boolean;
  runtime_id?: string;
  model_profile_id?: string;
  model_label?: string;
  model_switchable?: boolean;
  status?: string;
  active_task_count?: number;
  unavailable_reason?: string | null;
  models?: RuntimeModelProfile[];
}

export interface SessionModelBindingResponse {
  ok: boolean;
  source?: 'crew' | 'external' | 'team';
  scope?: 'team' | 'team_member';
  session_id?: string;
  external_team_id?: string;
  model_binding_revision?: number;
  model_profile_id?: string;
  pending_model_profile_id?: string | null;
  model_label?: string;
  pending_label?: string | null;
  has_pending?: boolean;
  pending?: boolean;
  /** 服务端判定：会话生效 Provider 为 FakeProvider 演示模式 */
  demo_mode?: boolean;
  models?: RuntimeModelProfile[];
  model_switchable?: boolean;
  runtime_id?: string;
  external_agent_id?: string;
  member_id?: string;
  member_name?: string;
  is_leader?: boolean;
  status?: string;
  active_task_count?: number;
  unavailable_reason?: string | null;
  members?: TeamMemberModelBinding[];
}

export interface AgentProfile {
  version: number;
  agent_id: string;
  availability: string;
  runtime: string;
  model?: {
    id: string;
    label: string;
    binding_status: 'valid' | 'missing' | 'unverified';
    capabilities: string[];
    thinking_levels: string[];
  };
}

export interface ExternalAgent {
  id: string;
  name: string;
  provider: string;
  display_badge?: string;
  runtime_id: string;
  model: string;
  system_prompt?: string;
  custom_args?: string[];
  custom_env?: Record<string, string>;
  description?: string;
  tags?: string[];
  status?: string;
  capabilities?: string[];
  sample_prompts?: string[];
  profile?: AgentProfile;
  profile_version?: number;
  profile_updated_at?: string | null;
  created_at?: string;
  updated_at?: string;
}

export interface ExternalTeamMember {
  id?: string;
  team_id?: string;
  agent_id: string;
  agent_name?: string;
  display_badge?: string;
  role?: string;
  role_key?: string;
  role_label?: string;
  capabilities?: string[];
  assigned_capabilities?: string[];
  workflow_lane?: string;
  sort_order?: number;
}

export interface ExternalTeam {
  id: string;
  name: string;
  display_badge?: string;
  description?: string;
  leader_agent_id?: string;
  instructions?: string;
  team_spec?: Record<string, unknown>;
  formation_plan?: FormationPlan;
  members?: ExternalTeamMember[];
  preset?: string;
  workflow?: string;
}

export interface ExternalTeamSuggestionMember {
  agent_id: string;
  role: string;
  role_key?: string;
  role_label?: string;
  capabilities?: string[];
  assigned_capabilities?: string[];
  responsibility?: Record<string, unknown>;
  responsibility_markdown?: string;
  workflow_lane?: string;
  selection_reason?: string;
  sort_order?: number;
}

export interface RequiredAgentConflict {
  agent_id: string;
  agent_name: string;
  required_capabilities: string[];
  matched_capabilities: string[];
  best_score: number;
  best_confidence?: number;
  reason: string;
}

export interface FormationPlanMember {
  agent_id: string;
  role_key: string;
  role_label: string;
  assigned_capabilities: string[];
  responsibility: Record<string, unknown>;
  responsibility_markdown: string;
  selection_source: string;
  locked: boolean;
  selection_reason: string;
}

export interface FormationPlan {
  version: number;
  leader_agent_id: string;
  members: FormationPlanMember[];
  coverage: { required: string[]; covered: string[]; uncovered: string[] };
  confidence: {
    requirement: number;
    capability_evidence: number;
    coverage: number;
    overall: number;
  };
  staffing_mode: string;
  excluded_agent_ids: string[];
  reasons: string[];
  warnings: string[];
}

export interface ExternalTeamSuggestion {
  leader_agent_id: string;
  workflow: string;
  members: ExternalTeamSuggestionMember[];
  requested_formation_mode: 'fast' | 'ai' | 'auto';
  selected_formation_mode: 'fast' | 'ai';
  fallback_reason: string;
  timing: {
    fast_ms: number;
    ai_ms: number;
    total_ms: number;
  };
  warnings: string[];
  reasons?: string[];
  team_spec?: Record<string, unknown>;
  formation_plan?: FormationPlan;
  decision_required?: boolean;
  required_agent_conflicts?: RequiredAgentConflict[];
  staffing_decision_required?: boolean;
  staffing_gaps?: FormationStaffingGap[];
  staffing_only_improvement?: boolean;
  ai_material_improvements?: string[];
}

export interface FormationStaffingGap {
  gap_id: string;
  role_key: string;
  role_label: string;
  required_capabilities: string[];
  responsibility_focus: string;
  reason: string;
  recommended_runtime_id: string;
  recommended_runtime_name: string;
  recommended_model_id: string;
}

export interface ExternalTeamDraft {
  description: string;
  workflow?: string;
  slots?: Array<Record<string, unknown>>;
}

export interface ExternalTeamDraftMeta {
  llmElapsedMs?: number;
  cacheHit?: boolean;
}

export interface ExternalTeamDraftStreamOptions {
  signal?: AbortSignal;
  onDescriptionDelta?: (text: string) => void;
  onDraft?: (draft: ExternalTeamDraft, phase: string, meta: ExternalTeamDraftMeta) => void;
}

export interface ExternalTeamSuggestionStreamOptions {
  signal?: AbortSignal;
  onSuggestion?: (suggestion: ExternalTeamSuggestion, phase: 'fast' | 'final') => void;
  onStatus?: (phase: 'ai_reviewing') => void;
}

export interface ExternalTeamRole {
  key: string;
  label: string;
  description: string;
  capabilities: string[];
  workflow_lane: string;
}

// ── Browser / Task / Cron / Dynamic Kanban / Followup ──

export interface BrowserPageState {
  owner_hash: string;
  session_hash: string;
  tab_id: string;
  tab_label: string;
  url: string;
  title: string;
  generation: number;
  mode: 'ai' | 'human' | 'paused';
  running: boolean;
  last_action: string;
  last_error: string;
  screenshot_id: string;
  viewport_width: number;
  viewport_height: number;
  can_go_back: boolean;
  can_go_forward: boolean;
  queue_depth?: number;
  last_queue_wait_ms?: number;
  last_operation_ms?: number;
  queue_timeouts?: number;
  tabs: Array<{ id: string; label: string; url: string; title: string }>;
  downloads: Array<{
    id?: string;
    name: string;
    path: string;
    created_at: number;
    state?: string;
    received_bytes?: number;
    total_bytes?: number;
    completed_at?: number;
    error?: string;
  }>;
}

export interface Task {
  id: string;
  task_id?: string;
  kind?: 'shell' | 'subagent' | 'agent_turn' | 'team';
  session_id?: string;
  title: string;
  detail?: string;
  assignee: string | null;
  status: string;
  result: string;
  error?: string;
  progress?: Record<string, unknown>;
  output_ref?: string;
  backgrounded?: boolean;
  auto_backgrounded?: boolean;
  created_at?: number;
  updated_at?: number;
  started_at?: number | null;
  finished_at?: number | null;
  last_activity_at?: number | null;
}

export interface CronJob {
  id: string;
  name: string;
  kind: string;
  trigger_type: string;
  trigger_payload: Record<string, unknown>;
  schedule: string;
  schedule_summary: string;
  query: string;
  session_id: string;
  workspace_id: string;
  deliver?: string;
  origin_source?: Record<string, unknown>;
  enabled: boolean;
  last_status: string;
  next_run_at: number;
  next_run_at_bj: string;
  last_run_at: number;
  last_run_at_bj: string;
  created_at?: number;
  created_at_bj?: string;
  timezone: string;
}

export interface CronJobList {
  jobs: CronJob[];
  count: number;
  timezone: string;
}

export interface CronJobRun {
  id: string;
  job_id: string;
  started_at: number;
  started_at_bj: string;
  finished_at: number | null;
  finished_at_bj: string;
  status: string;
  error_message: string;
  duration_seconds: number | null;
}

export interface CronJobDetail {
  ok: boolean;
  job: CronJob;
  runs: CronJobRun[];
  run_summary: { total: number; success: number; failed: number; other: number };
  timezone: string;
}

export interface CronDeliveryTarget {
  id: string;
  label: string;
  platform: string;
}

export interface WorkflowPhase {
  id: string;
  name: string;
  description?: string;
  max_concurrent?: number;
  agent_calls?: unknown[];
  verification_gate?: {
    role: string;
    prompt: string;
    pass_key?: string;
    fallback_phase_id?: string;
    max_retries?: number;
  } | null;
}

export interface DynamicKanbanStatus {
  workflow: { status?: string; [k: string]: unknown } | null;
  workflow_definition?: {
    summary?: string;
    max_concurrent?: number;
    phases?: WorkflowPhase[];
  } | null;
  runtime_state: {
    workflow_id: string;
    status: string;
    current_phase_id: string;
    completed_phase_ids: string[];
    phase_results?: Record<
      string,
      {
        status?: string;
        verification_result?: { reason?: string; [k: string]: unknown };
        call_results?: Record<
          string,
          {
            status?: string;
            role?: string;
            text?: string;
            error?: string;
            artifacts?: string[];
          }
        >;
      }
    >;
    variables?: Record<string, unknown>;
    pause_requested?: boolean;
    pause_reason?: string;
    loop_count?: number;
    updated_at?: number;
  } | null;
  board: {
    workflow_id: string;
    tasks: unknown[];
    dependencies: unknown[];
    events: unknown[];
  };
}

/** 追问选择框：后端 ask_followup_question 工具推过来的交互内容。 */
export interface FollowupQuestion {
  question_id: string;
  title: string;
  record_history?: boolean;
  status?: string;
  note?: string;
  origin?: {
    type?: string;
    agent_name?: string;
    origin_session_id?: string;
    mention_intent?: string;
  };
  questions: {
    id: string;
    question: string;
    options: Array<{ label: string; value: string; description?: string }>;
    allowFreeText?: boolean;
    multiSelect: boolean;
  }[];
}

/** 追问答案：每个子问题的选择（含自定义输入文本），回传后端 followup_answer action。 */
export interface FollowupAnswer {
  question_id: string;
  answers: string[];
}

// ── 外部 Team 流式辅助函数 ──

async function streamExternalTeamDraft(
  path: string,
  payload: object,
  options?: ExternalTeamDraftStreamOptions,
): Promise<ExternalTeamDraft> {
  const init: RequestInit = { method: 'POST', ...jsonBody(payload) };
  if (options?.signal) init.signal = options.signal;
  const res = await gatewayFetch(path, init);
  const text = await res.text();
  if (!res.ok) {
    throw new Error(`团队草案生成失败：${res.status}`);
  }
  let latest: ExternalTeamDraft = { description: '' };
  for (const rawLine of text.split('\n')) {
    const line = rawLine.trim();
    if (!line) continue;
    let event: Record<string, unknown>;
    try {
      event = JSON.parse(line) as Record<string, unknown>;
    } catch {
      continue;
    }
    if (event.type === 'description_delta' && typeof event.text === 'string') {
      options?.onDescriptionDelta?.(event.text);
      latest = { ...latest, description: event.text };
      continue;
    }
    if (event.type === 'draft' && event.draft && typeof event.draft === 'object') {
      latest = event.draft as ExternalTeamDraft;
      options?.onDraft?.(latest, String(event.phase || ''), {
        ...(typeof event.llm_elapsed_ms === 'number' ? { llmElapsedMs: event.llm_elapsed_ms } : {}),
        ...(typeof event.cache_hit === 'boolean' ? { cacheHit: event.cache_hit } : {}),
      });
    }
  }
  return latest;
}

/** Stream Fast draft → AI review/final, preferring Electron's cancellable bridge. */
async function streamExternalTeamSuggestion(
  payload: object,
  options?: ExternalTeamSuggestionStreamOptions,
): Promise<ExternalTeamSuggestion> {
  const bridge = CrewBridge();
  if (
    typeof bridge?.gatewayStreamStart === 'function'
    && typeof bridge.gatewayStreamCancel === 'function'
    && typeof bridge.onGatewayStreamEvent === 'function'
  ) {
    return streamExternalTeamSuggestionBridge(payload, options, {
      gatewayStreamStart: bridge.gatewayStreamStart,
      gatewayStreamCancel: bridge.gatewayStreamCancel,
      onGatewayStreamEvent: bridge.onGatewayStreamEvent,
    });
  }
  const init: RequestInit = {
    method: 'POST',
    ...jsonBody({ ...payload, formation_mode: 'auto' }),
  };
  if (options?.signal) init.signal = options.signal;
  const res = await gatewayFetch('/api/external-teams/suggest', init);
  if (!res.ok) {
    throw new Error(`智能组队失败：${res.status}`);
  }
  if (options?.signal?.aborted) throw new DOMException('Aborted', 'AbortError');
  let latest: ExternalTeamSuggestion | null = null;
  const text = await res.text();
  for (const rawLine of text.split('\n')) {
    if (options?.signal?.aborted) throw new DOMException('Aborted', 'AbortError');
    const line = rawLine.trim();
    if (!line) continue;
    let event: {
      type?: string;
      phase?: string;
      suggestion?: ExternalTeamSuggestion;
    };
    try {
      event = JSON.parse(line) as typeof event;
    } catch {
      continue;
    }
    if (event.type === 'status' && event.phase === 'ai_reviewing') {
      options?.onStatus?.('ai_reviewing');
      continue;
    }
    if (
      event.type === 'suggestion'
      && (event.phase === 'fast' || event.phase === 'final')
      && event.suggestion
    ) {
      latest = event.suggestion;
      options?.onSuggestion?.(event.suggestion, event.phase);
    }
  }
  if (!latest) throw new Error('智能组队流没有返回有效方案');
  return latest;
}

/** Pair one bridge request with one listener and cancel both on abort or settlement. */
async function streamExternalTeamSuggestionBridge(
  payload: object,
  options: ExternalTeamSuggestionStreamOptions | undefined,
  bridge: Required<Pick<
    CrewBridge,
    'gatewayStreamStart' | 'gatewayStreamCancel' | 'onGatewayStreamEvent'
  >>,
): Promise<ExternalTeamSuggestion> {
  const requestId = `formation-${crypto.randomUUID()}`;
  const url = `${gatewayBase()}/api/external-teams/suggest`;
  const init = {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ...payload, formation_mode: 'auto' }),
  };
  let latest: ExternalTeamSuggestion | null = null;
  let buffer = '';
  let settled = false;

  return new Promise<ExternalTeamSuggestion>((resolve, reject) => {
    const consumeLines = () => {
      const lines = buffer.split('\n');
      buffer = lines.pop() || '';
      for (const rawLine of lines) {
        const line = rawLine.trim();
        if (!line) continue;
        const event = JSON.parse(line) as {
          type?: string;
          phase?: string;
          suggestion?: ExternalTeamSuggestion;
        };
        if (event.type === 'status' && event.phase === 'ai_reviewing') {
          options?.onStatus?.('ai_reviewing');
        } else if (
          event.type === 'suggestion'
          && (event.phase === 'fast' || event.phase === 'final')
          && event.suggestion
        ) {
          latest = event.suggestion;
          options?.onSuggestion?.(event.suggestion, event.phase);
        }
      }
    };
    const cleanup = bridge.onGatewayStreamEvent((event) => {
      if (event.request_id !== requestId || settled) return;
      try {
        if (event.type === 'chunk') {
          buffer += event.text || '';
          consumeLines();
          return;
        }
        if (event.type === 'error') {
          settled = true;
          cleanup();
          reject(new Error(event.error || 'Gateway 流请求失败'));
          return;
        }
        if (event.type === 'end') {
          if (buffer.trim()) {
            buffer += '\n';
            consumeLines();
          }
          settled = true;
          cleanup();
          if (latest) resolve(latest);
          else reject(new Error('智能组队流没有返回有效方案'));
        }
      } catch (error) {
        settled = true;
        cleanup();
        void bridge.gatewayStreamCancel(requestId);
        reject(error);
      }
    });
    const abort = () => {
      if (settled) return;
      settled = true;
      cleanup();
      void bridge.gatewayStreamCancel(requestId);
      reject(new DOMException('Aborted', 'AbortError'));
    };
    options?.signal?.addEventListener('abort', abort, { once: true });
    if (options?.signal?.aborted) {
      abort();
      return;
    }
    void bridge.gatewayStreamStart(requestId, url, init).catch((error) => {
      if (settled) return;
      settled = true;
      cleanup();
      reject(error);
    });
  });
}

// ---------------------------------------------------------------------------
// BackendChatSocket：Gateway WebSocket 连接与消息封装
// ---------------------------------------------------------------------------

/**
 * WS 重连退避：1.5s 起 ×2 封顶 30s，±20% 抖动（避免多端同时重连打点）。
 * 连接成功（open）清零。纯函数，便于单测。
 */
export function computeWsReconnectDelayMs(attempts: number): number {
  const base = Math.min(30_000, 1_500 * (2 ** Math.min(Math.max(attempts, 0), 5)));
  return Math.round(base * (0.8 + Math.random() * 0.4));
}

export class BackendChatSocket {
  private ws: WebSocket | null = null;
  private closed = false;
  private usingGatewayProxy = false;
  private gatewayProxyOpen = false;
  private connectInFlight = false;
  private unsubscribeGatewayProxy: (() => void) | null = null;
  private reconnectTimer: number | null = null;
  private reconnectAttempts = 0;
  private subscribedSessions = new Set<string>();
  /** 重连 resubscribe 时解析各 session 的 last_gateway_sequences。 */
  private resolveLastGatewaySequences: ((sessionIds: string[]) => Record<string, number>) | undefined;

  constructor(
    private readonly onChunk: (chunk: ChatChunk) => void,
    private readonly onStatus: (open: boolean, meta?: BackendSocketStatusMeta) => void,
    private readonly onOpen?: () => void,
  ) {}

  /** 供单测 / 诊断：当前 proxy 通道是否已 open。 */
  isGatewayProxyOpen(): boolean {
    return this.gatewayProxyOpen;
  }

  /** 注入 gateway_sequence 解析器（由 session-controller 在 bootstrap 时绑定）。 */
  bindLastGatewaySequences(resolver: (sessionIds: string[]) => Record<string, number>): void {
    this.resolveLastGatewaySequences = resolver;
  }

  /** 指数退避重连（成功 open 后 attempts 清零）。 */
  private scheduleReconnect(): void {
    if (this.closed) return;
    const delay = computeWsReconnectDelayMs(this.reconnectAttempts);
    this.reconnectAttempts += 1;
    this.reconnectTimer = window.setTimeout(() => this.connect(), delay);
  }

  connect(): void {
    if (this.closed) return;
    if (this.reconnectTimer !== null) {
      window.clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    const bridge = CrewBridge();
    if (bridge?.gatewayWsConnect && bridge?.gatewayWsSend && bridge?.onGatewayWsEvent) {
      if (this.usingGatewayProxy && this.gatewayProxyOpen) {
        logStream('ws-renderer', 'connect-skip-already-open', {});
        return;
      }
      if (this.connectInFlight) {
        logStream('ws-renderer', 'connect-skip-in-flight', {});
        return;
      }
      this.usingGatewayProxy = true;
      this.connectInFlight = true;
      this.gatewayProxyOpen = false;
      logStream('ws-renderer', 'connect-via-proxy', {});
      this.unsubscribeGatewayProxy?.();
      this.unsubscribeGatewayProxy = bridge.onGatewayWsEvent((event) => {
        const payload = event as { type?: string; data?: string; error?: string; code?: number; reason?: string };
        if (payload.type === 'open') {
          this.connectInFlight = false;
          this.gatewayProxyOpen = true;
          this.reconnectAttempts = 0;
          logStream('ws-renderer', 'proxy-event-open', {});
          this.onStatus(true);
          this.resubscribe();
          this.onOpen?.();
          return;
        }
        if (payload.type === 'message') {
          // 坏帧只丢弃自身（计数 + 日志），不允许在事件回调里炸成 uncaught。
          let frame: ChatChunk | null = null;
          try {
            frame = JSON.parse(String(payload.data || '{}')) as ChatChunk;
          } catch (err) {
            reportRendererError('ws-frame', err, { via: 'proxy', textLen: String(payload.data ?? '').length });
            return;
          }
          if (frame?.kind === 'ping') {
            queueMicrotask(() => {
              void this.send({ kind: 'pong' });
            });
            return;
          }
          if (frame?.kind === 'security_approval') {
            window.dispatchEvent(new CustomEvent('security:approval-pending', { detail: frame }));
            return;
          }
          logStream('ws-renderer', 'proxy-event-message', {
            kind: frame.kind,
            request_id: frame.request_id,
            session_id: frame.session_id,
            sequence: frame.sequence,
            is_final: frame.is_final,
            textLen: typeof frame.body?.text === 'string' ? frame.body.text.length : undefined,
          });
          this.onChunk(frame);
          return;
        }
        if (payload.type === 'error') {
          logStream('ws-renderer', 'proxy-event-error', { error: payload.error });
          this.gatewayProxyOpen = false;
          this.onStatus(false);
          return;
        }
        if (payload.type === 'close') {
          const reason = String(payload.reason ?? '');
          const transient = reason === 'reconnect';
          logStream('ws-renderer', 'proxy-event-close', { code: payload.code, reason, transient });
          this.connectInFlight = false;
          this.gatewayProxyOpen = false;
          this.onStatus(false, { transient });
          if (!transient) {
            this.scheduleReconnect();
          }
        }
      });
      void bridge.gatewayWsConnect().then((result: { ok?: boolean }) => {
        this.connectInFlight = false;
        if (!result?.ok) {
          logStream('ws-renderer', 'proxy-connect-failed', { result });
          this.gatewayProxyOpen = false;
          this.onStatus(false);
          // ensureGateway 冷启动超时/失败时 connect 会直接 fail；gateway 稍后就绪
          // 后若这里不重试，会一直停在「服务未连接」。与 close 路径同样退避重连。
          this.scheduleReconnect();
        }
      });
      return;
    }
    logStream('ws-renderer', 'connect-direct-ws', { wsBase: wsBase() });
    const wsUrl = `${wsBase()}/ws`;
    this.ws = new WebSocket(wsUrl);
    this.ws.onopen = () => {
      this.reconnectAttempts = 0;
      this.onStatus(true);
      this.resubscribe();
      this.onOpen?.();
    };
    this.ws.onmessage = (event) => {
      let payload: ChatChunk | null = null;
      try {
        payload = JSON.parse(event.data) as ChatChunk;
      } catch (err) {
        reportRendererError('ws-frame', err, { via: 'direct' });
        return;
      }
      if (payload?.kind === 'ping') {
        queueMicrotask(() => {
          void this.send({ kind: 'pong' });
        });
        return;
      }
      logStream('ws-renderer', 'direct-ws-message', {
        kind: payload.kind,
        request_id: payload.request_id,
        session_id: payload.session_id,
      });
      this.onChunk(payload);
    };
    this.ws.onerror = () => this.onStatus(false);
    this.ws.onclose = () => {
      this.onStatus(false);
      this.scheduleReconnect();
    };
  }

  async send(
    payload:
      | SendPayload
      | {
          action: string;
          session_id: string;
          text?: string;
          sessions?: string[];
          last_gateway_sequences?: Record<string, number>;
          answers?: Record<string, unknown>;
          request_id?: string;
          mode?: string;
          workspace_id?: string;
          /** 看板手改 / 批准时附带的计划正文 */
          plan?: string;
          /** Wiki 模式（wiki_enter）：目标知识库与联网搜索开关 */
          kb_id?: string;
          web_search_enabled?: boolean;
        }
      | { action: 'followup_answer'; session_id: string; question_id: string; answers: FollowupAnswer[] }
      | { action: 'followup_cancel'; session_id: string; question_id: string }
      | { kind: 'pong' },
  ): Promise<boolean> {
    if (this.usingGatewayProxy) {
      const bridge = CrewBridge();
      if (!this.gatewayProxyOpen || !bridge?.gatewayWsSend) {
        return false;
      }
      const result = await bridge.gatewayWsSend(payload);
      if (!result?.ok) {
        this.gatewayProxyOpen = false;
        this.onStatus(false);
        return false;
      }
      return true;
    }
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
      return false;
    }
    this.ws.send(JSON.stringify(payload));
    return true;
  }

  private resubscribe(): void {
    const sessions = Array.from(this.subscribedSessions);
    if (sessions.length > 0) {
      const seqs = this.resolveLastGatewaySequences?.(sessions);
      const hasSeqs = seqs && Object.keys(seqs).length > 0;
      void this.send({
        action: 'subscribe',
        session_id: sessions[0],
        sessions,
        ...(hasSeqs ? { last_gateway_sequences: seqs } : {}),
      });
    }
  }

  subscribe(sessionIds: string[], lastGatewaySequences?: Record<string, number>): Promise<boolean> {
    const sessions = Array.from(new Set(sessionIds.map((id) => id.trim()).filter(Boolean)));
    sessions.forEach((id) => this.subscribedSessions.add(id));
    if (sessions.length === 0) return Promise.resolve(true);
    const hasSeqs = lastGatewaySequences && Object.keys(lastGatewaySequences).length > 0;
    return this.send({
      action: 'subscribe',
      session_id: sessions[0],
      sessions,
      ...(hasSeqs ? { last_gateway_sequences: lastGatewaySequences } : {}),
    });
  }

  /**
   * 从本地订阅集移除会话。**不向 gateway 发送 unsubscribe**：当前 gateway
   * (crew/gateway/ws.py) 未实现该动作，且未识别动作会被当作空 query 的对话回合
   * `_spawn`，造成幽灵 turn。仅客户端裁剪 Set——下次重连 `resubscribe()` 只发
   * 剩余会话，gateway 状态随之收敛。修 P2-1 的内存增长 + 重连全量重发带宽。
   */
  unsubscribe(sessionIds: string[]): void {
    const toRemove = new Set(sessionIds.map((id) => id.trim()).filter(Boolean));
    toRemove.forEach((id) => this.subscribedSessions.delete(id));
  }

  /** 当前订阅的会话 id（调试 / 单测用）。 */
  getSubscribedSessions(): string[] {
    return Array.from(this.subscribedSessions);
  }

  stop(sessionId: string): Promise<boolean> {
    return this.send({ action: 'stop', session_id: sessionId });
  }

  interrupt(sessionId: string): Promise<boolean> {
    return this.send({ action: 'interrupt', session_id: sessionId });
  }

  steer(sessionId: string, text: string): Promise<boolean> {
    return this.send({ action: 'steer', session_id: sessionId, text });
  }
  /** 转后台：对应 ws.py 的 background action，把当前运行任务转为后台任务并返回 task_id。 */
  background(sessionId: string): Promise<boolean> {
    return this.send({ action: 'background', session_id: sessionId });
  }
  planEnter(sessionId: string): Promise<boolean> {
    return this.send({ action: 'plan_enter', session_id: sessionId });
  }

  planExit(sessionId: string): Promise<boolean> {
    return this.send({ action: 'plan_exit', session_id: sessionId });
  }

  /** 进入 Wiki 模式（Phase 4）：对齐 web ws.ts wikiEnter，kb_id / web_search_enabled 可选。 */
  wikiEnter(sessionId: string, kbId?: string, webSearchEnabled?: boolean): Promise<boolean> {
    return this.send({
      action: 'wiki_enter',
      session_id: sessionId,
      ...(kbId ? { kb_id: kbId } : {}),
      ...(webSearchEnabled ? { web_search_enabled: true } : {}),
    });
  }

  /** 退出 Wiki 模式（Phase 4）。 */
  wikiExit(sessionId: string): Promise<boolean> {
    return this.send({ action: 'wiki_exit', session_id: sessionId });
  }

  planReject(sessionId: string): Promise<boolean> {
    return this.send({ action: 'plan_reject', session_id: sessionId });
  }

  planRejectAndExit(sessionId: string): Promise<boolean> {
    return this.send({ action: 'plan_reject_and_exit', session_id: sessionId });
  }

  /** 看板手改：把计划正文写回服务端 plan 文件，并期望回推 plan_review。 */
  planUpdate(sessionId: string, plan: string): Promise<boolean> {
    return this.send({ action: 'plan_update', session_id: sessionId, plan });
  }

  /**
   * 批准计划；可选附带最新正文（看板手改后原子落盘+批准）。
   * `plan` 有值时后端先 update 再 approve。
   */
  planApprove(
    sessionId: string,
    mode: string,
    workspaceId: string,
    requestId?: string,
    plan?: string,
  ): Promise<boolean> {
    return this.send({
      action: 'plan_approve',
      session_id: sessionId,
      mode,
      workspace_id: workspaceId,
      ...(requestId ? { request_id: requestId } : {}),
      ...(typeof plan === 'string' ? { plan } : {}),
    });
  }

  dispose(): void {
    this.closed = true;
    if (this.reconnectTimer !== null) {
      window.clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    this.ws?.close();
    if (this.usingGatewayProxy) {
      void CrewBridge()?.gatewayWsClose?.();
      this.usingGatewayProxy = false;
      this.gatewayProxyOpen = false;
    }
    this.unsubscribeGatewayProxy?.();
    this.unsubscribeGatewayProxy = null;
    // 清空订阅集，防止 dispose 后残留引用 + 重连（如果复用实例）时全量重发旧会话
    this.subscribedSessions.clear();
  }
}

// ---------------------------------------------------------------------------
// Session API 片段（由 backend-client.ts 聚合为 backendApi）
// ---------------------------------------------------------------------------

export const sessionApi = {
  getSessionModel: (sessionId: string) =>
    getJSON<SessionModelBindingResponse>(`/api/session/${encodeURIComponent(sessionId)}/model`),

  setSessionModel: (
    sessionId: string,
    modelProfileId: string,
    opts?: { workspace_id?: string; title?: string; member_id?: string },
  ) =>
    getJSON<SessionModelBindingResponse>(`/api/session/${encodeURIComponent(sessionId)}/model`, {
      method: 'PUT',
      ...jsonBody({
        model_profile_id: modelProfileId,
        workspace_id: opts?.workspace_id,
        title: opts?.title,
        member_id: opts?.member_id,
      }),
    }),

  sessions: (workspaceId?: string, opts?: { includeArchived?: boolean }) => {
    const params = new URLSearchParams();
    if (workspaceId) params.set('workspace_id', workspaceId);
    if (opts?.includeArchived) params.set('include_archived', 'true');
    const q = params.toString();
    return getJSON<BackendSession[]>(`/api/sessions${q ? `?${q}` : ''}`);
  },
  ensureSession: (id: string, payload: { workspace_id: string; title?: string }) =>
    getJSON<{ ok: boolean; session_id: string }>(`/api/session/${encodeURIComponent(id)}/ensure`, {
      method: 'POST',
      ...jsonBody(payload),
    }),
  channelSessions: () =>
    getJSON<{
      platforms: Array<{
        platform: string;
        label: string;
        sessions: Array<{
          session_id: string;
          title?: string;
          updated_at: number;
          workspace_id?: string;
          platform?: string;
        }>;
      }>;
    }>('/api/channel-sessions'),
  sessionsStatus: () => getJSON<Record<string, string>>('/api/sessions/status'),
  history: (id: string) => getJSON<BackendHistoryItem[]>(`/api/session/${encodeURIComponent(id)}`),
  /** 窗口化历史（P1-1）：首屏尾部窗口 + 游标翻页；before 为上一页 next_before 原样回传。 */
  historyWindow: (id: string, options: { limit?: number; before?: string | null } = {}) => {
    const params = new URLSearchParams();
    if (options.limit) params.set('limit', String(options.limit));
    if (options.before) params.set('before', options.before);
    const query = params.toString();
    return getJSON<{
      items: BackendHistoryItem[];
      has_more: boolean;
      next_before: string | null;
    }>(
      `/api/session/${encodeURIComponent(id)}/history${query ? `?${query}` : ''}`,
    );
  },
  sessionPlan: (id: string) =>
    getJSON<SessionPlanState>(`/api/session/${encodeURIComponent(id)}/plan`),
  sessionStatus: (id: string) =>
    getJSON<{
      session_id: string;
      live: string;
      active_request_id?: string | null;
      queue_depth: number;
      last_status: string;
      last_error: string;
    }>(
      `/api/session/${encodeURIComponent(id)}/status`,
    ),
  renameSession: (id: string, title: string) =>
    getJSON<{ ok: boolean }>(`/api/session/${encodeURIComponent(id)}/title`, {
      method: 'PUT',
      ...jsonBody({ title }),
    }),
  deleteSession: (id: string) =>
    getJSON<{ ok: boolean }>(`/api/session/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  archiveSession: (id: string, archived: boolean) =>
    getJSON<{ ok: boolean; archived: boolean }>(`/api/session/${encodeURIComponent(id)}/archive`, {
      method: 'PUT',
      ...jsonBody({ archived }),
    }),
  pinSession: (id: string, pinned: boolean) =>
    getJSON<{ ok: boolean; pinned: boolean }>(`/api/session/${encodeURIComponent(id)}/pin`, {
      method: 'PUT',
      ...jsonBody({ pinned }),
    }),

  tasks: (sessionId: string) => getJSON<Task[]>(`/api/tasks?session_id=${encodeURIComponent(sessionId)}`),
  cancelTask: (taskId: string, reason = '用户取消') =>
    getJSON<Task>(`/api/tasks/${encodeURIComponent(taskId)}/cancel`, {
      method: 'POST',
      body: JSON.stringify({ reason }),
      headers: { 'Content-Type': 'application/json' },
    }),
  recoverTeamNode: (
    sessionId: string,
    nodeId: string,
    action: 'reassign' | 'retry' | 'abandon',
    replacementAssignee = '',
  ) =>
    getJSON<{ ok: boolean; node?: Task; error?: string }>(
      `/api/session/${encodeURIComponent(sessionId)}/team/recover`,
      {
        method: 'POST',
        body: JSON.stringify({
          node_id: nodeId,
          action,
          replacement_assignee: replacementAssignee,
        }),
        headers: { 'Content-Type': 'application/json' },
      },
    ),
  cronJobs: (sessionId?: string) => {
    const q = sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : '';
    return getJSON<CronJobList>(`/api/cron/jobs${q}`);
  },
  cronDeliveryTargets: () =>
    getJSON<{ ok: boolean; targets: CronDeliveryTarget[] }>('/api/cron/delivery-targets'),
  createCronJob: (payload: { name: string; schedule: string; query: string; session_id: string; deliver?: string; origin_source?: Record<string, unknown> }) =>
    getJSON<CronJob>('/api/cron/jobs', { method: 'POST', ...jsonBody(payload) }),
  pauseCronJob: (id: string) => getJSON<CronJob>(`/api/cron/jobs/${encodeURIComponent(id)}/pause`, { method: 'POST' }),
  resumeCronJob: (id: string) => getJSON<CronJob>(`/api/cron/jobs/${encodeURIComponent(id)}/resume`, { method: 'POST' }),
  deleteCronJob: (id: string) => getJSON<{ ok: boolean; id: string }>(`/api/cron/jobs/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  cronRunNow: (id: string) =>
    getJSON<{ ok: boolean; job?: CronJob; run?: { id: string; status: string; session_id: string; next_run_at?: number; enabled?: boolean } }>(
      `/api/cron/jobs/${encodeURIComponent(id)}/run`,
      { method: 'POST' },
    ),
  cronJobDetail: (id: string, limit = 20) =>
    getJSON<CronJobDetail>(`/api/cron/jobs/${encodeURIComponent(id)}?limit=${limit}`),

  // 用量统计
  usage: () => getJSON<{ total_tokens?: number; prompt_tokens?: number; completion_tokens?: number; total_cost?: number; sessions?: number }>('/api/usage'),
  sessionContext: (sessionId: string) =>
    getJSON<{
      available: boolean;
      used_tokens: number | null;
      max_tokens: number;
      ratio: number | null;
      source: 'provider' | 'request_view' | 'preview' | 'unavailable';
      warning?: string;
    }>(`/api/session/${encodeURIComponent(sessionId)}/context`),
  browserState: (sessionId: string) =>
    getJSON<{ ok: boolean; state: BrowserPageState }>(`/api/browser/${encodeURIComponent(sessionId)}/state`),
  /** 读取指定标签页的正文（@ 提及标签页、存入 Wiki 共用）。ok:false 时带 error。 */
  browserReadTab: (sessionId: string, tabId: string) =>
    getJSON<{ ok: boolean; title: string; url: string; text: string; error?: string }>(
      `/api/browser/${encodeURIComponent(sessionId)}/read-tab`,
      { method: 'POST', ...jsonBody({ tab_id: tabId }) },
    ),
  browserControl: (sessionId: string, action: string, value = '') =>
    getJSON<{
      ok: boolean;
      state: BrowserPageState;
      result?: string;
    }>(`/api/browser/${encodeURIComponent(sessionId)}/control`, {
      method: 'POST',
      ...jsonBody({ action, value }),
    }),
  browserOpenArtifact: (sessionId: string, path: string, newTab = false) =>
    getJSON<{ ok: boolean; state: BrowserPageState }>(`/api/browser/${encodeURIComponent(sessionId)}/artifact`, {
      method: 'POST',
      ...jsonBody({ path, new_tab: newTab }),
    }),
  browserClearData: () =>
    getJSON<{ ok: boolean; cleared: boolean; owner_hash: string }>('/api/browser/data', {
      method: 'DELETE',
    }),
  sessionTodos: (sessionId: string) =>
    getJSON<{ todos: Array<{ id: string; content: string; status: string }> }>(`/api/session/${encodeURIComponent(sessionId)}/todos`),
  // 运行时（外部 Agent runtime 检测）
  runtimes: () => getJSON<ExternalRuntime[]>('/api/runtimes'),
  scanRuntimes: () => getJSON<ExternalRuntime[]>('/api/runtimes/scan', { method: 'POST' }),
  deleteRuntime: (id: string) =>
    getJSON<{ ok: boolean }>(`/api/runtimes/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  // 外部 Agent：作为外援页的实时数据源
  externalAgents: () => getJSON<ExternalAgent[]>('/api/external-agents'),
  createExternalAgent: (agent: {
    name: string;
    runtime_id: string;
    model?: string;
    system_prompt?: string;
    instructions?: string;
    custom_args?: string[];
    custom_env?: Record<string, string>;
  }) => getJSON<ExternalAgent>('/api/external-agents', { method: 'POST', ...jsonBody(agent) }),
  deleteExternalAgent: (id: string) =>
    getJSON<{ ok: boolean }>(`/api/external-agents/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  externalTeams: () => getJSON<ExternalTeam[]>('/api/external-teams'),
  externalTeamRoles: () => getJSON<ExternalTeamRole[]>('/api/external-teams/roles'),
  createExternalTeam: (team: {
    name: string;
    description?: string;
    leader_agent_id: string;
    instructions?: string;
    workflow?: string;
    team_spec?: Record<string, unknown>;
    formation_plan?: FormationPlan;
    temporary_members?: {
      gap_id?: string;
      name?: string;
      role_key: string;
      required_capabilities?: string[];
      responsibility_focus?: string;
      reason?: string;
      runtime_id: string;
      model_id: string;
    }[];
    members: {
      agent_id: string;
      role: string;
      role_key?: string;
      role_label?: string;
      capabilities?: string[];
      assigned_capabilities?: string[];
      workflow_lane?: string;
      sort_order?: number;
    }[];
  }) => getJSON<ExternalTeam>('/api/external-teams', { method: 'POST', ...jsonBody(team) }),
  draftExternalTeamDescription: (
    payload: { name?: string },
    options?: ExternalTeamDraftStreamOptions,
  ) => streamExternalTeamDraft('/api/external-teams/draft/description', payload, options),
  suggestExternalTeam: (payload: {
    name?: string;
    description?: string;
    workflow?: string;
    leader_agent_id?: string;
    formation_mode: 'fast' | 'ai';
    required_agent_ids?: string[];
    excluded_agent_ids?: string[];
    force_required_agent_ids?: string[];
    required_capabilities?: string[];
    custom_capabilities?: string[];
  }) => getJSON<ExternalTeamSuggestion>('/api/external-teams/suggest', { method: 'POST', ...jsonBody(payload) }),
  suggestExternalTeamAuto: (
    payload: {
      name?: string;
      description?: string;
      workflow?: string;
      leader_agent_id?: string;
      required_agent_ids?: string[];
      excluded_agent_ids?: string[];
      force_required_agent_ids?: string[];
      required_capabilities?: string[];
      custom_capabilities?: string[];
    },
    options?: ExternalTeamSuggestionStreamOptions,
  ) => streamExternalTeamSuggestion(payload, options),
  deleteExternalTeam: (id: string) =>
    getJSON<{ ok: boolean }>(`/api/external-teams/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  // 会话 agent-config：写入选中的外援 / 执行器
  getSessionAgentConfig: (sessionId: string) =>
    getJSON<SessionAgentConfig>(`/api/session/${encodeURIComponent(sessionId)}/agent-config`),
  setSessionAgentConfig: (sessionId: string, config: SessionAgentConfig) =>
    getJSON<{ ok: boolean; [k: string]: unknown }>(`/api/session/${encodeURIComponent(sessionId)}/agent-config`, {
      method: 'PUT',
      ...jsonBody({ config }),
    }),
  // 运行并发（后端 dispatcher.runtime_status() 字段名）
  runtimeConcurrency: () => getJSON<{ max_active_runs: number; global_active: number; global_queued: number; sessions?: Record<string, unknown>; active_children?: unknown }>('/api/runtime/concurrency'),

  dynamicKanbanBoard: (sessionId: string) =>
    getJSON<{ workflow?: Record<string, unknown>; tasks: unknown[]; dependencies: unknown[]; events: unknown[] }>(
      `/api/dynamic-kanban/${encodeURIComponent(sessionId)}/board`,
    ),
  dynamicKanbanStatus: (sessionId: string) =>
    getJSON<DynamicKanbanStatus>(`/api/dynamic-kanban/${encodeURIComponent(sessionId)}/status`),
  dynamicKanbanPause: (sessionId: string, reason = '用户请求暂停') =>
    getJSON<{ ok: boolean; session_id: string; reason: string }>(
      `/api/dynamic-kanban/${encodeURIComponent(sessionId)}/pause?reason=${encodeURIComponent(reason)}`,
      { method: 'POST' },
    ),
  dynamicKanbanResume: (sessionId: string) => readDynamicKanbanResumeStream(sessionId),
};
