/**
 * 后端 HTTP 客户端：gateway 后端（FastAPI /api/...）的类型化封装。
 *
 * - 默认 base URL 读取 localStorage 键 `Crew.gatewayBase`，
 *   缺省回落到 `http://127.0.0.1:8000`。
 * - 桌面端通过 `window.Crew.gatewayFetch` 走 Electron 主进程 IPC，
 *   避免浏览器 CORS / 端口限制；web 端直接走 fetch。
 * - 错误处理：识别 HTML 响应（典型为 SPA fallback）和 JSON 错误体（提取 error / detail），
 *   把 HTTP 状态码 + 后端错误信息合成人类可读消息抛给 UI 层。
 */
import { logStream } from './stream-debug';

export type Mode = 'agent' | 'team' | 'dynamic_kanban';
export type ChunkKind =
  | 'delta'
  | 'tool'
  | 'task'
  | 'status'
  | 'final'
  | 'error'
  | 'thinking'
  | 'plan_review'
  | 'followup_question'
  | 'kanban'
  | 'todo_updated'
  | 'file_changes'
  | 'workflow_progress'
  | 'team_internal'
  | 'session_title'
  | 'channel_session_updated'
  | 'cron_session_created'
  | 'cron_session_updated'
  | 'audit_updated'
  | 'work_event'
  | 'wiki_ingest_progress'
  | 'wiki_cards'
  | 'wiki_summary'
  | 'wiki_changed'
  | 'ping'
  | 'pong';

/** 写入 /api/session/{id}/agent-config 的 payload。 */
export interface SessionAgentConfig {
  executor: 'builtin' | 'client' | 'external' | 'acp' | 'team';
  external_agent_id?: string;
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
  capabilities?: string[];
  thinking_levels?: string[];
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
  agent_binding?: SessionAgentBinding;
}

export interface WikiAgentSessionSummary {
  session_id: string;
  title: string;
  message_count: number;
  updated_at: number;
  created_at?: number;
  workspace_id: 'wiki';
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
  name?: string;
  message_id?: string;
  origin?: {
    source?: string;
    sender_kind?: 'human' | 'agent';
    sender_id?: string;
    sender_name?: string;
    is_self?: boolean;
    delivery_state?: string;
  };
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

export interface ModelOption {
  id: string;
  name: string;
  model: string;
  base_url?: string;
  api_key_env?: string;
  provider?: string;
  has_key: boolean;
  temperature?: number;
  max_tokens?: number | null;
  context_window?: number | null;
  timeout?: number;
  loaded: boolean;
  builtin?: boolean;
  capabilities?: string[];
}

export interface ModelPayload {
  id?: string;
  name?: string;
  api_key?: string;
  api_key_env?: string;
  provider?: string;
  base_url?: string;
  model?: string;
  temperature?: number;
  max_tokens?: number | null;
  context_window?: number | null;
  timeout?: number;
  loaded?: boolean;
  capabilities?: string[];
}

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

export interface BackendConfig {
  model: string;
  has_key: boolean;
  base_url: string;
  active_model_id: string;
  models: ModelOption[];
  model_profiles?: ModelOption[];
  is_gateway_admin?: boolean;
  external_agents?: {
    enabled?: boolean;
  };
  security?: {
    enabled?: boolean;
    default_mode?: 'request_approval' | 'auto_review' | 'full_access';
  };
}

export interface Workspace {
  id: string;
  name: string;
  description: string;
  instructions: string;
  root_path?: string;
  hidden?: boolean;
  created_at?: number;
  updated_at?: number;
}

export interface LocalSite {
  id: string;
  workspace_id: string;
  session_id: string;
  name: string;
  description: string;
  source_path: string;
  build_command: string;
  output_directory: string;
  active_release_id: string;
  created_at: number;
  updated_at: number;
}

export interface SiteAnnotation {
  id: string;
  site_id: string;
  release_id: string;
  route: string;
  selector: string;
  element_tag: string;
  element_text: string;
  comment: string;
  context: Record<string, unknown>;
  status: 'open' | 'resolved' | 'rejected';
  created_at: number;
  updated_at: number;
}

export interface InspirationItem {
  id: string;
  kind: 'site' | 'canvas';
  title: string;
  description: string;
  workspaceId: string;
  sessionId: string;
  createdAt: number;
  updatedAt: number;
}

export interface InspirationAnnotation {
  id: string;
  inspirationId: string;
  inspirationKind: 'site' | 'canvas' | 'widget';
  targetKind: 'site_dom' | 'canvas' | 'widget' | 'widget_dom';
  canvasId: string;
  widgetId: string;
  mountId: string;
  revisionId: string;
  route: string;
  selector: string;
  elementTag: string;
  elementText: string;
  comment: string;
  context: Record<string, unknown>;
  status: 'open' | 'resolved' | 'rejected';
  createdAt: number;
  updatedAt: number;
}

export interface InspirationDetail extends InspirationItem {
  site?: LocalSite;
  canvas?: BlueprintCanvas;
  widgets?: Record<string, BlueprintWidget>;
  annotations: InspirationAnnotation[];
}

export interface InspirationSurface {
  kind: 'inspiration';
  mode: 'site' | 'canvas' | 'widget';
  inspirationId?: string;
  siteId?: string;
  canvasId?: string;
  widgetId?: string;
  sessionId: string;
  title: string;
  status?: 'preparing' | 'ready';
  revisionId?: string;
  resourceRevision?: number;
}

export interface BlueprintLayout {
  mode: 'grid' | 'free'; x: number; y: number; w: number; h: number;
}

export interface CanvasPlacement {
  mountId: string; canvasId: string; widgetId: string; layout: BlueprintLayout;
  zOrder: number; viewState: Record<string, unknown>; createdAt: number; updatedAt: number;
}

export interface BlueprintCanvas {
  id: string; workspaceId: string; sessionId: string; title: string; purpose: string;
  widgetCount?: number; placements?: CanvasPlacement[]; createdAt: number; updatedAt: number;
}

export interface BlueprintWidget {
  id: string; workspaceId: string; title: string; description: string; workspacePath: string;
  slots: Record<string, unknown>; events: Record<string, unknown>;
  latestData: Record<string, unknown>; status: string; error: string; lastRun: string;
  bindings: { main?: string }; createdAt: number; updatedAt: number;
  resourceRevision: number;
  validation?: { status: 'valid' | 'invalid'; issues: Array<{ code: string; message: string }>; entry: string };
}

export interface Attachment {
  id: string;
  name: string;
  path: string;
  type: 'image' | 'file';
  size?: number;
}

export interface CompanionConversationBinding {
  kind: 'nearby_dm' | 'nearby_room';
  target_id: string;
  session_id: string;
  workspace_id: string;
  title: string;
  capabilities: {
    can_send_text: boolean;
    can_attach: boolean;
    can_mention_people: boolean;
    can_mention_agents: boolean;
    show_model_picker: boolean;
    show_skills: boolean;
    show_plan_mode: boolean;
  };
}

export interface CompanionAgentCandidate {
  source_ref: string;
  source_kind: 'builtin' | 'external';
  source_id: string;
  display_name: string;
  description: string;
  provider: string;
  available: boolean;
  published: boolean;
  public_agent_id: string;
}

export interface CompanionPreparedFile {
  file_id: string;
  name: string;
  path: string;
  type: 'image' | 'file';
  mime_type: string;
  size: number;
  sha256: string;
  data_base64: string;
}

export interface Skill {
  name: string;
  slug: string;
  aliases?: string[];
  description: string;
  source: 'builtin' | 'user';
  /** 是否从本机共享 Skill 目录接入；移除时保留原始 Skill。 */
  is_local_shared?: boolean;
  /** SKILL.md frontmatter category；缺省为「通用」。 */
  category?: string;
  /** 中文名（来自 metadata.zh_name，后端 display_name 字段）；缺省回退 name。 */
  display_name?: string;
  /** 中文描述（来自 metadata.zh_description，后端 description_zh 字段）；缺省回退 description。 */
  description_zh?: string;
}

export interface OptionalSkill {
  name: string;
  slug: string;
  aliases?: string[];
  description: string;
  category: string;
  source: 'optional' | 'local';
  /** 中文名（来自 metadata.zh_name，后端 display_name 字段）；缺省回退 name。 */
  display_name?: string;
  /** 中文描述（来自 metadata.zh_description，后端 description_zh 字段）；缺省回退 description。 */
  description_zh?: string;
}

export interface EvolutionConfig {
  auto_trigger: boolean;
  auto_full_cycle: boolean;
  visible: boolean;
}

export interface SkillStore {
  installed: Skill[];
  optional: OptionalSkill[];
  /** ~/.agents/skills 中未安装的本地 skill（跨 agent 共享，软链安装）。 */
  local?: OptionalSkill[];
  evolution?: EvolutionConfig;
}

/** /api/tools 返回的单个工具行（外援等工具选择器用）。 */
export interface ToolInfo {
  name: string;
  toolset: string;
  display_name?: string;
}

export interface SubScenario {
  id: string;
  title: string;
  query: string;
}

export interface Scenario {
  id: string;
  title: string;
  icon?: string;
  description?: string;
  category?: string;
  items: SubScenario[];
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

export interface PlatformRow {
  name: string;
  label: string;
  available: boolean;
  configured: boolean;
  connected: boolean;
  enabled?: boolean;
  running?: boolean;
  live_connected?: boolean;
  error?: string;
  error_kind?: 'network' | string;
  description?: string;
  install_hint?: string;
  detail?: Record<string, unknown>;
  operation?: string;
  reason?: 'login_required' | 'disconnected' | 'error' | string;
  has_account?: boolean;
}

export interface PlatformConfigResponse {
  ok: boolean;
  name: string;
  config: Record<string, unknown>;
  secret_fields: string[];
  has_secret: Record<string, boolean>;
  has_account: boolean;
  environment?: string;
  presets?: Array<{ id: string; label: string }>;
  status?: PlatformRow;
}

export interface PlatformSavePayload {
  enabled: boolean;
  config: Record<string, unknown>;
  secrets?: Record<string, string>;
  environment?: string;
}

export interface CompleteItem {
  text: string;
  display: string;
  meta: string;
  type: string;
}

/** /api/system/metrics 返回的宿主机 + 进程资源指标。 */
export interface SystemMetrics {
  uptime_s: number;
  cpu_count: number;
  cpu_percent?: number;
  memory?: { total_gb: number; used_gb: number; percent: number };
  disk?: { total_gb: number; used_gb: number; free_gb: number; percent: number };
  network?: { bytes_sent: number; bytes_recv: number };
  process?: { rss_mb: number; pid: number };
  psutil_unavailable?: boolean;
}

/** /api/system/logs 返回的单条日志。 */
export interface LogEntry {
  ts: number;
  level: string;
  name: string;
  message: string;
}

export interface ChatChunk {
  kind: ChunkKind | 'security_approval';
  body: Record<string, unknown>;
  is_final: boolean;
  sequence: number;
  request_id?: string;
  session_id?: string;
  /** Gateway 侧单调序号，用于断线 replay 与客户端去重。 */
  gateway_sequence?: number;
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

export interface SendPayload {
  query: string;
  session_id: string;
  request_id?: string;
  mode: Mode;
  workspace_id: string;
  attachments?: Attachment[];
  sub_scenario?: string;
  client_intent?: 'revision';
  /** Wiki 模式：本轮携带的 KB 与联网搜索开关（对齐 web useChat 的字段名）。 */
  wiki_kb_id?: string;
  web_search_enabled?: boolean;
  /** Work 模式：仅本轮不应用的偏好 ID。 */
  work_disabled_preference_ids?: string[];
}

const DEFAULT_GATEWAY = 'http://127.0.0.1:8000';

type CrewBridge = {
  gatewayFetch?: typeof gatewayFetchBridge;
  gatewayStreamStart?: (
    requestId: string,
    url: string,
    init?: { method?: string; headers?: Record<string, string>; body?: string },
  ) => Promise<{ ok?: boolean }>;
  gatewayStreamCancel?: (requestId: string) => Promise<{ ok?: boolean }>;
  onGatewayStreamEvent?: (cb: (event: {
    request_id: string;
    type: 'head' | 'chunk' | 'end' | 'error';
    status?: number;
    headers?: Record<string, string>;
    text?: string;
    error?: string;
  }) => void) => () => void;
  gatewayUpload?: (url: string, files: string[]) => Promise<GatewayUploadResult>;
  gatewayWsConnect?: () => Promise<{ ok?: boolean; status?: number; error?: string }>;
  gatewayWsSend?: (payload: unknown) => Promise<{ ok?: boolean; error?: string }>;
  gatewayWsClose?: () => Promise<{ ok?: boolean }>;
  onGatewayWsEvent?: (cb: (event: unknown) => void) => () => void;
};

function CrewBridge(): CrewBridge | undefined {
  return (window as Window & { Crew?: CrewBridge }).Crew;
}

function gatewayBase(): string {
  return localStorage.getItem('Crew.gatewayBase') || DEFAULT_GATEWAY;
}

function wsBase(): string {
  return gatewayBase().replace(/^http:/, 'ws:').replace(/^https:/, 'wss:');
}

async function gatewayFetch(path: string, opts?: RequestInit): Promise<Response> {
  const url = `${gatewayBase()}${path}`;
  const bridge = CrewBridge()?.gatewayFetch;
  if (typeof bridge === 'function') {
    return gatewayFetchBridge(url, opts);
  }
  return fetch(url, opts);
}

async function gatewayFetchBridge(url: string, opts?: RequestInit): Promise<Response> {
  const headers: Record<string, string> = {};
  if (opts?.headers) {
    const h = opts.headers as Record<string, string>;
    Object.assign(headers, h);
  }
  const body = typeof opts?.body === 'string' ? opts.body : undefined;
  const initArg: { method?: string; headers: Record<string, string>; body?: string } = { headers };
  if (opts?.method !== undefined) initArg.method = opts.method;
  if (body !== undefined) initArg.body = body;
  const result = await window.Crew.gatewayFetch(url, initArg);
  return new Response(result.body, {
    status: result.status,
    statusText: result.statusText,
    headers: result.headers,
  });
}

/** gateway:upload IPC 单个文件的结果（与 gateway:fetch 返回 shape 一致）。 */
export interface GatewayUploadFileResult {
  path: string;
  ok: boolean;
  status: number;
  statusText: string;
  body: string;
  headers: Record<string, string>;
}

export interface GatewayUploadResult {
  results: GatewayUploadFileResult[];
}

/** 统一的 JSON 响应解析：错误体提取 error/message，HTML 响应识别为 SPA fallback。 */
async function readJsonResponse<T>(res: Response, path: string): Promise<T> {
  const text = await res.text();
  if (!res.ok) {
    let message = `${res.status} ${path}`;
    try {
      const body = text ? JSON.parse(text) : null;
      const msg = body?.error || body?.message;
      if (msg) message = String(msg);
    } catch {
      // 非 JSON 错误体保留 HTTP 状态即可，避免把 HTML 泄露到界面。
    }
    throw new Error(message);
  }
  const contentType = res.headers.get('content-type') || '';
  if (!contentType.toLowerCase().includes('application/json')) {
    const preview = text.trim().slice(0, 32).toLowerCase();
    if (preview.startsWith('<!doctype') || preview.startsWith('<html')) {
      throw new Error(`当前服务未提供接口 ${path}，请重启后端服务后重试。`);
    }
    throw new Error(`接口 ${path} 返回了无法识别的数据。`);
  }
  try {
    return JSON.parse(text) as T;
  } catch {
    throw new Error(`接口 ${path} 返回的数据格式异常。`);
  }
}

async function getJSON<T>(path: string, opts?: RequestInit): Promise<T> {
  return readJsonResponse<T>(await gatewayFetch(path, opts), path);
}

/**
 * 桌面端本地文件上传：走主进程 gateway:upload IPC
 * （gateway:fetch 桥不透传二进制 body）。一次一个文件，与后端逐文件接收对齐。
 */
async function uploadJSON<T>(path: string, filePath: string): Promise<T> {
  const bridge = CrewBridge()?.gatewayUpload;
  if (typeof bridge !== 'function') {
    throw new Error('当前环境不支持本地文件上传（需要桌面端）。');
  }
  const result = await bridge(`${gatewayBase()}${path}`, [filePath]);
  const item = result?.results?.[0];
  if (!item) {
    throw new Error('上传失败：主进程未返回结果。');
  }
  const res = new Response(item.body, {
    // Response 构造只接受 200-599；本地失败合成体均在此范围，这里兜底钳制。
    status: item.status >= 200 && item.status <= 599 ? item.status : 500,
    statusText: item.statusText,
    headers: item.headers,
  });
  return readJsonResponse<T>(res, path);
}

const jsonBody = (body: object): RequestInit => ({
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
});

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

/** 拼接 wiki 接口的 kb_id query 参数（无 kbId 时后端回落 default KB）。 */
function withKb(path: string, kbId?: string): string {
  return kbId ? `${path}${path.includes('?') ? '&' : '?'}kb_id=${encodeURIComponent(kbId)}` : path;
}

/** 解析 Dynamic Kanban resume 的 SSE 流，产出 ChatChunk。 */
async function* readDynamicKanbanResumeStream(sessionId: string): AsyncGenerator<ChatChunk> {
  // gatewayFetch 内部会拼接 gatewayBase()，这里只传 path（传完整 URL 会拼成双重 base，
  // 导致桌面端 IPC 校验报 "url: not a valid URL"）。
  const path = `/api/dynamic-kanban/${encodeURIComponent(sessionId)}/resume`;
  const res = await gatewayFetch(path, { method: 'POST' });
  if (!res.ok) {
    let message = `恢复 workflow 失败: ${res.status}`;
    try {
      const body = await res.text();
      const parsed = body ? JSON.parse(body) : null;
      if (parsed?.error || parsed?.message) message = String(parsed.error || parsed.message);
    } catch {
      // ignore
    }
    throw new Error(message);
  }

  // 桌面端 IPC 可能直接返回完整文本 body，ReadableStream 不一定可用
  const text = await res.text().catch(() => '');
  const lines = text.split('\n');
  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed.startsWith('data: ')) continue;
    const data = trimmed.slice(6).trim();
    if (data === '[DONE]') return;
    if (!data) continue;
    try {
      const parsed = JSON.parse(data) as ChatChunk;
      if (parsed.request_id && !parsed.session_id) {
        parsed.session_id = sessionId;
      }
      yield parsed;
    } catch {
      // 忽略无法解析的行
    }
  }
}

/** 当前账号可见的插件与三层开关状态。 */
export interface PluginItem {
  name: string;
  key: string;
  label: string;
  version: string;
  description: string;
  kind: string;
  enabled: boolean;
  installed: boolean;
  system_allowed: boolean;
  role_allowed: boolean;
  user_enabled: boolean;
  user_enabled_explicit: boolean;
  effective_enabled: boolean;
  runtime_ready?: boolean;
  runtime_state?: {
    ready: boolean;
    closing: boolean;
    actions_blocked: boolean;
    stop_unconfirmed: boolean;
  };
  toggle_endpoint?: string | null;
  tools: string[];
  hooks: string[];
  platforms: string[];
  error?: string | null;
}

/** MCP server 传输类型。 */
export type McpTransport = 'stdio' | 'http' | 'sse' | 'unknown';

/** MCP server 运行时配置（密钥类 env 已脱敏为 ***）。 */
export interface McpServerConfig {
  command?: string;
  args?: string[];
  env?: Record<string, string>;
  url?: string;
  transport?: McpTransport;
  headers?: Record<string, string>;
}

/** 后端 /api/mcp/servers 返回的单个 server 行。 */
export interface McpServerRow {
  name: string;
  transport: McpTransport;
  connected: boolean;
  error: string;
  tools: string[];
  config: McpServerConfig;
}

/** 新增/编辑 MCP server 的 payload（前端表单 → 后端）。 */
export interface McpServerPayload {
  name?: string;
  command?: string;
  args?: string[];
  env?: Record<string, string>;
  url?: string;
  transport?: McpTransport;
  headers?: Record<string, string>;
}

/** CUA Driver 安装任务的某个步骤进度。 */
export interface CuaSetupStep {
  name: string;
  status: string; // pending / running / success / failed / skipped
  message: string;
  ts: number;
}

/** GET /api/mcp/cua-driver/setup/{task_id} 返回的安装进度。 */
export interface CuaSetupProgress {
  task_id: string;
  platform: string;
  status: string; // pending / running / success / failed / cancelled
  started_at: number;
  finished_at: number | null;
  steps: CuaSetupStep[];
  log: string[];
  error: string | null;
}

/** GET /api/mcp/cua-driver/status 返回的当前安装/运行状态。 */
export interface CuaDriverStatus {
  ok: boolean;
  installed: boolean;
  binary: string | null;
  version: string;
  daemon_running: boolean;
  mcp_enabled: boolean;
  tools_registered: string[];
}

// ── LLM Wiki 类型（与 web/src/types.ts 同形，为后续上传/图谱等阶段铺路）──

export type WikiPageStatus = 'published' | 'deprecated';
export type WikiPageType = 'entity' | 'concept' | 'topic' | 'source' | 'comparison' | 'synthesis';
export type WikiConfidence = 'high' | 'medium' | 'low';
export type WikiSourceType = 'upload' | 'url' | 'session' | 'paste' | 'image' | 'video';
export type WikiParseStatus = 'pending' | 'parsed' | 'failed';
export type WikiViewMode = 'timeline' | 'tree' | 'type' | 'graph';

export interface WikiKB {
  id: string;
  name: string;
  created_at: number;
  updated_at: number;
}

export interface WikiPage {
  id: string;
  page_type: WikiPageType;
  title: string;
  /** 完整正文；列表接口（brief=1）可能不返回。 */
  content?: string;
  /** 列表摘要（brief=1 时由后端生成），优先用于左侧列表展示。 */
  summary?: string;
  file_path: string;
  sources: string[];
  related: string[];
  status: WikiPageStatus;
  tags: string[];
  created_at: number;
  updated_at: number;
  aliases: string[];
  claims?: WikiClaim[];
  claim_count?: number;
  confidence?: WikiConfidence | null;
  contested?: boolean;
  contradictions?: string[];
  relations?: WikiRelation[];
}

export interface WikiRelation { target: string; relation: string; }
export interface WikiEvidence { source_id: string; locator?: string; excerpt?: string; }
export interface WikiClaim {
  statement: string;
  evidence: WikiEvidence[];
  confidence: WikiConfidence;
  contested: boolean;
  contradictions: string[];
}

export interface WikiSource {
  id: string;
  title: string;
  source_type: WikiSourceType;
  original_path?: string;
  parsed_path?: string;
  file_type?: string;
  size: number;
  created_at: number;
  session_id?: string;
  parse_status: WikiParseStatus;
  parse_error?: string;
  original_sha256?: string;
  content_sha256?: string;
  drift_from?: string;
  is_duplicate: boolean;
  source_url?: string;
}

/** source_id -> 人类可读标题 的映射 */
export type WikiSourceTitles = Record<string, string>;

export interface WikiRelationPage {
  id: string;
  title: string;
  page_type: WikiPageType;
  relation: string;
  direction: 'outgoing' | 'incoming';
}

export interface WikiSourcePage {
  id: string;
  title: string;
  page_type: WikiPageType;
}

/** source_id -> 原始文件元信息 的映射 */
export type WikiSourceFiles = Record<string, { original_path: string; file_type?: string; title?: string }>;

export interface WikiVaultDocument {
  name: 'Home.md' | 'index.md';
  path: string;
  content: string;
  updated_at: number;
}

export interface WikiGraph {
  nodes: WikiGraphNode[];
  edges: WikiGraphEdge[];
}

export interface WikiGraphNode {
  id: string;
  title: string;
  type: WikiPageType | 'source';
}

export interface WikiGraphEdge {
  source: string;
  target: string;
  relation: string;
}

export interface WikiIngestProgress {
  stage: string;
  percent: number;
  label: string;
  source_id: string;
  session_id?: string;
  error?: string;
  detail?: Record<string, unknown>;
}

/** wiki_ingest_progress 帧：/api/wiki/ingest 编译期间经 WS 推送的进度（body 为 WikiIngestProgress）。 */
export interface WikiIngestProgressChunk extends Omit<ChatChunk, 'kind' | 'body'> {
  kind: 'wiki_ingest_progress';
  body: WikiIngestProgress;
}

/** wiki_cards 帧：Wiki Agent 回合结束后经 WS 推送的引用页面卡片（body.pages 为 WikiPage 数组）。 */
export interface WikiCardsChunk extends Omit<ChatChunk, 'kind' | 'body'> {
  kind: 'wiki_cards';
  body: { pages?: WikiPage[]; cards?: WikiPage[] };
}

/** wiki_summary 帧：进入 Wiki 模式时后端推送的 KB 概览卡（body 为 WikiSummary）。 */
export interface WikiSummaryChunk extends Omit<ChatChunk, 'kind' | 'body'> {
  kind: 'wiki_summary';
  body: WikiSummary;
}

export interface WikiUploadResult {
  ok: boolean;
  source_id: string;
  title: string;
  source_type?: 'upload' | 'image' | 'video';
  ingested?: boolean;
  needs_confirmation?: boolean;
  /** 解析失败但文件已保存（交给 Wiki Agent 挽救）时返回。 */
  needs_agent_review?: boolean;
  error?: string;
  message?: string;
  pages?: WikiPage[];
  issues?: string[];
}

export interface WikiSummary {
  summary: string;
  kb_id: string;
  page_count?: number;
  source_count?: number;
  generated_at?: number;
  status: 'ready' | 'generating' | 'empty' | 'stale';
}

export const backendApi = {
  companionProfile: () => getJSON<{
    profile: Record<string, unknown>;
    public_profile: Record<string, unknown>;
    agent_candidates: CompanionAgentCandidate[];
  }>('/api/companion/profile'),
  companionUpdatePublications: (publishedAgentRefs: string[]) => getJSON<{
    profile: Record<string, unknown>;
    public_profile: Record<string, unknown>;
    agent_candidates: CompanionAgentCandidate[];
  }>('/api/companion/profile', {
    method: 'PUT',
    ...jsonBody({ published_agent_refs: publishedAgentRefs }),
  }),
  companionConversations: () => getJSON<{
    conversations: CompanionConversationBinding[];
    peers: Array<Record<string, unknown>>;
    rooms: Array<Record<string, unknown>>;
  }>('/api/companion/conversations'),
  companionOpenConversation: (payload: {
    kind: 'nearby_dm' | 'nearby_room';
    target_id: string;
    workspace_id?: string;
    title?: string;
  }) => getJSON<{ ok: boolean } & CompanionConversationBinding>(
    '/api/companion/conversations/open',
    { method: 'POST', ...jsonBody(payload) },
  ),
  companionSendMessage: (
    sessionId: string,
    text: string,
    mentions: string[] = [],
    attachments: Attachment[] = [],
  ) =>
    getJSON<{ ok: boolean; event_id: string; status: string }>(
      `/api/companion/conversations/${encodeURIComponent(sessionId)}/messages`,
      { method: 'POST', ...jsonBody({ text, mentions, attachments }) },
    ),
  companionPrepareFile: (attachment: Attachment) =>
    getJSON<{ ok: boolean; file: CompanionPreparedFile }>(
      '/api/companion/files/prepare',
      { method: 'POST', ...jsonBody(attachment) },
    ),
  companionSettleOutbox: (
    eventId: string,
    status: 'queued' | 'sending' | 'sent' | 'delivered' | 'failed',
  ) =>
    getJSON<{ ok: boolean; status: string }>(
      `/api/companion/outbox/${encodeURIComponent(eventId)}/settle`,
      { method: 'POST', ...jsonBody({ status }) },
    ),
  companionLinkState: (event: Record<string, unknown>) =>
    getJSON<{
      ok: boolean;
      appended?: boolean;
      binding?: CompanionConversationBinding;
      attachment?: Attachment;
    }>('/api/companion/link-state', {
      method: 'POST',
      ...jsonBody(event),
    }),
  inspirations: () => getJSON<{ ok: boolean; inspirations: InspirationItem[] }>(
    '/api/sites/inspirations',
  ),
  inspiration: (inspirationId: string) => getJSON<{ ok: boolean; inspiration: InspirationDetail }>(
    `/api/sites/inspirations/${encodeURIComponent(inspirationId)}`,
  ),
  deleteInspiration: (inspirationId: string) => getJSON<{ ok: boolean }>(
    `/api/sites/inspirations/${encodeURIComponent(inspirationId)}`, { method: 'DELETE' },
  ),
  exportInspiration: (inspirationId: string) => getJSON<{
    ok: boolean; archive_path: string; filename: string;
  }>(`/api/sites/inspirations/${encodeURIComponent(inspirationId)}/export`, {
    method: 'POST', ...jsonBody({}),
  }),
  createInspirationAnnotation: (inspirationId: string, payload: Record<string, unknown>) =>
    getJSON<{ ok: boolean; annotation: InspirationAnnotation }>(
      `/api/sites/inspirations/${encodeURIComponent(inspirationId)}/annotations`,
      { method: 'POST', ...jsonBody(payload) },
    ),
  updateInspirationAnnotation: (
    inspirationId: string, annotationId: string, status: InspirationAnnotation['status'],
  ) => getJSON<{ ok: boolean; annotation: InspirationAnnotation }>(
    `/api/sites/inspirations/${encodeURIComponent(inspirationId)}/annotations/${encodeURIComponent(annotationId)}`,
    { method: 'PATCH', ...jsonBody({ status }) },
  ),
  canvases: () => getJSON<{ ok: boolean; canvases: BlueprintCanvas[] }>('/api/sites/canvases'),
  canvas: (canvasId: string) => getJSON<{
    ok: boolean; canvas: BlueprintCanvas; widgets: Record<string, BlueprintWidget>;
  }>(`/api/sites/canvases/${encodeURIComponent(canvasId)}`),
  updateCanvasPlacement: (canvasId: string, mountId: string, payload: Record<string, unknown>) =>
    getJSON<{ ok: boolean; placement: CanvasPlacement }>(
      `/api/sites/canvases/${encodeURIComponent(canvasId)}/placements/${encodeURIComponent(mountId)}`,
      { method: 'PATCH', ...jsonBody(payload) },
    ),
  widget: (widgetId: string) => getJSON<{ ok: boolean; widget: BlueprintWidget }>(
    `/api/sites/widgets/${encodeURIComponent(widgetId)}`,
  ),
  emitWidget: (widgetId: string, value: unknown = null) => getJSON<{
    ok: boolean; run: Record<string, unknown>; widget: BlueprintWidget;
  }>(`/api/sites/widgets/${encodeURIComponent(widgetId)}/emit`, {
    method: 'POST', ...jsonBody({ name: 'submit', value }),
  }),
  sites: (workspaceId?: string) => getJSON<{ ok: boolean; sites: LocalSite[] }>(
    `/api/sites${workspaceId ? `?workspace_id=${encodeURIComponent(workspaceId)}` : ''}`,
  ),
  site: (siteId: string) => getJSON<{
    ok: boolean; site: LocalSite; releases: Array<Record<string, unknown>>; annotations: SiteAnnotation[];
  }>(`/api/sites/${encodeURIComponent(siteId)}`),
  publishSite: (siteId: string) => getJSON<{ ok: boolean; site: LocalSite }>(
    `/api/sites/${encodeURIComponent(siteId)}/publish`, { method: 'POST', ...jsonBody({}) },
  ),
  deleteSite: (siteId: string) => getJSON<{ ok: boolean }>(
    `/api/sites/${encodeURIComponent(siteId)}`, { method: 'DELETE' },
  ),
  createSiteAnnotation: (siteId: string, payload: Record<string, unknown>) =>
    getJSON<{ ok: boolean; annotation: SiteAnnotation }>(
      `/api/sites/${encodeURIComponent(siteId)}/annotations`, { method: 'POST', ...jsonBody(payload) },
    ),
  updateSiteAnnotation: (siteId: string, annotationId: string, status: SiteAnnotation['status']) =>
    getJSON<{ ok: boolean; annotation: SiteAnnotation }>(
      `/api/sites/${encodeURIComponent(siteId)}/annotations/${encodeURIComponent(annotationId)}`,
      { method: 'PATCH', ...jsonBody({ status }) },
    ),
  exportSite: (siteId: string) => getJSON<{ ok: boolean; archive_path: string; filename: string }>(
    `/api/sites/${encodeURIComponent(siteId)}/export`, { method: 'POST', ...jsonBody({}) },
  ),
  config: () => getJSON<BackendConfig>('/api/config'),
  switchModel: (modelId: string) =>
    getJSON<BackendConfig>('/api/config/model', { method: 'POST', ...jsonBody({ model_id: modelId }) }),
  createModel: (payload: ModelPayload) =>
    getJSON<BackendConfig & { ok: boolean; profile: ModelOption }>('/api/config/models', {
      method: 'POST',
      ...jsonBody(payload),
    }),
  updateModel: (modelId: string, payload: ModelPayload) =>
    getJSON<BackendConfig & { ok: boolean; profile: ModelOption }>(`/api/config/models/${encodeURIComponent(modelId)}`, {
      method: 'PUT',
      ...jsonBody(payload),
    }),
  deleteModel: (modelId: string, opts?: { force?: boolean }) => {
    const q = opts?.force ? '?force=true' : '';
    return getJSON<{ ok: boolean; removed: ModelOption; active_model_id: string; models: ModelOption[]; switched_to?: string; rebound_sessions?: string[]; busy_sessions?: string[] }>(
      `/api/config/models/${encodeURIComponent(modelId)}${q}`,
      { method: 'DELETE' },
    );
  },

  getSessionModel: (sessionId: string) =>
    getJSON<{
      ok: boolean;
      source?: 'crew' | 'external';
      model_profile_id: string;
      pending_model_profile_id?: string | null;
      model_label?: string;
      pending_label?: string | null;
      has_pending?: boolean;
      models?: RuntimeModelProfile[];
      model_switchable?: boolean;
      runtime_id?: string;
      external_agent_id?: string;
    }>(`/api/session/${encodeURIComponent(sessionId)}/model`),

  setSessionModel: (
    sessionId: string,
    modelProfileId: string,
    opts?: { workspace_id?: string; title?: string },
  ) =>
    getJSON<{
      ok: boolean;
      source?: 'crew' | 'external';
      model_profile_id: string;
      pending_model_profile_id?: string | null;
      model_label?: string;
      pending_label?: string | null;
      has_pending?: boolean;
      pending?: boolean;
      models?: RuntimeModelProfile[];
      model_switchable?: boolean;
      runtime_id?: string;
      external_agent_id?: string;
    }>(`/api/session/${encodeURIComponent(sessionId)}/model`, {
      method: 'PUT',
      ...jsonBody({
        model_profile_id: modelProfileId,
        workspace_id: opts?.workspace_id,
        title: opts?.title,
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

  workspaces: () => getJSON<Workspace[]>('/api/workspaces'),
  createWorkspace: (fields: Partial<Workspace>) =>
    getJSON<Workspace>('/api/workspaces', { method: 'POST', ...jsonBody(fields) }),
  updateWorkspace: (id: string, fields: Partial<Workspace>) =>
    getJSON<Workspace>(`/api/workspace/${encodeURIComponent(id)}`, { method: 'PUT', ...jsonBody(fields) }),
  deleteWorkspace: (id: string) =>
    getJSON<{ ok: boolean }>(`/api/workspace/${encodeURIComponent(id)}`, { method: 'DELETE' }),

  tasks: (sessionId: string) => getJSON<Task[]>(`/api/tasks?session_id=${encodeURIComponent(sessionId)}`),
  cancelTask: (taskId: string, reason = '用户取消') =>
    getJSON<Task>(`/api/tasks/${encodeURIComponent(taskId)}/cancel`, {
      method: 'POST',
      body: JSON.stringify({ reason }),
      headers: { 'Content-Type': 'application/json' },
    }),
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

  // 定时任务（crew/cron 后端存储）

  // 用量统计
  usage: () => getJSON<{ total_tokens?: number; prompt_tokens?: number; completion_tokens?: number; total_cost?: number; sessions?: number }>('/api/usage'),
  sessionContext: (sessionId: string) =>
    getJSON<{ used_tokens: number; max_tokens: number; ratio: number }>(`/api/session/${encodeURIComponent(sessionId)}/context`),
  browserState: (sessionId: string) =>
    getJSON<{ ok: boolean; state: BrowserPageState }>(`/api/browser/${encodeURIComponent(sessionId)}/state`),
  // record_* 动作返回 `recording` 而不是 `state`：录制态与页面控制态是正交的
  // 两件事，录制不改变 ControlMode，所以不复用 state 字段。
  browserControl: (sessionId: string, action: string, value = '') =>
    getJSON<{
      ok: boolean;
      state: BrowserPageState;
      result?: string;
      // record_discard 的返回：是否真的删掉了轨迹文件
      discarded?: boolean;
      recording?: {
        recording: boolean;
        paused: boolean;
        steps: number;
        incomplete?: boolean;
        dropped_steps?: number;
        recording_id?: string;
        note?: string;
        // 停止录制时一并返回：用户点「生成技能」之前要能看到自己要交出什么。
        summary?: {
          steps: number;
          hosts: string[];
          notes: string[];
          masked_fields: number;
          handoff_fields: number;
          pages_captured: number;
          incomplete?: boolean;
          dropped_steps?: number;
        };
      };
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

  // 系统监控
  systemMetrics: () => getJSON<SystemMetrics>('/api/system/metrics'),
  systemLogs: (params: { level?: string | undefined; q?: string | undefined; limit?: number | undefined; offset?: number | undefined } = {}) => {
    const sp = new URLSearchParams();
    if (params.level) sp.set('level', params.level);
    if (params.q) sp.set('q', params.q);
    if (params.limit) sp.set('limit', String(params.limit));
    if (params.offset) sp.set('offset', String(params.offset));
    const qs = sp.toString();
    return getJSON<{ items: LogEntry[]; total: number }>(`/api/system/logs${qs ? `?${qs}` : ''}`);
  },

  upload: (filename: string, contentBase64: string, opts?: { sessionId?: string | undefined; kbId?: string | undefined }) => {
    const body: { filename: string; content: string; session_id?: string; kb_id?: string } = {
      filename,
      content: contentBase64,
    };
    if (opts?.sessionId) body.session_id = opts.sessionId;
    if (opts?.kbId) body.kb_id = opts.kbId;
    return getJSON<Attachment>('/api/upload', { method: 'POST', ...jsonBody(body) });
  },
  complete: (query: string, opts?: { cwd?: string; workspaceId?: string }) => {
    const params = new URLSearchParams({ query });
    if (opts?.cwd) params.set('cwd', opts.cwd);
    if (opts?.workspaceId) params.set('workspace_id', opts.workspaceId);
    return getJSON<CompleteItem[]>(`/api/complete?${params}`);
  },

  skills: () => getJSON<Skill[]>('/api/skills'),
  skillStore: () => getJSON<SkillStore>('/api/skills/store'),
  installSkill: (slug: string) =>
    getJSON<{ ok: boolean }>(`/api/skills/${encodeURIComponent(slug)}/install`, { method: 'POST' }),
  uninstallSkill: (slug: string) =>
    getJSON<{ ok: boolean }>(`/api/skills/${encodeURIComponent(slug)}`, { method: 'DELETE' }),
  /** 从 base64 zip 安装远程技能（Skill Hub）。网关本地解压落盘，不触外网。 */
  installFromZip: (slug: string, content: string, version?: string, hubId?: string) => {
    const body: { slug: string; content: string; version?: string; hub_id?: string } = { slug, content };
    if (version) body.version = version;
    if (hubId) body.hub_id = hubId;
    return getJSON<{ ok: boolean; slug?: string; name?: string }>('/api/skills/install-from-zip', {
      method: 'POST',
      ...jsonBody(body),
    });
  },
  /** 读取远程 Skill Hub 安装时写入的 .hub-meta.json 侧车；本地技能返回 {}。 */
  skillMeta: (slug: string) =>
    getJSON<{ hubId?: string; version?: string }>(`/api/skills/${encodeURIComponent(slug)}/meta`),

  /** 更新自进化配置 */
  updateEvolution: (config: Partial<EvolutionConfig>) =>
    getJSON<{ ok: boolean; evolution: EvolutionConfig }>('/api/skills/evolution', {
      method: 'PUT',
      ...jsonBody(config),
    }),

  toolsets: () => getJSON<string[]>('/api/toolsets'),
  tools: () => getJSON<ToolInfo[]>('/api/tools'),

  scenarios: (count = 4) => getJSON<Scenario[]>(`/api/scenarios?count=${count}`),
  scenarioIntroLines: (count = 8) => getJSON<string[]>(`/api/scenarios/intro-lines?count=${count}`),
  scenarioLoadingStatuses: (count = 8) => getJSON<string[]>(`/api/scenarios/loading-status?count=${count}`),

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

  plugins: () => getJSON<PluginItem[]>('/api/plugins'),
  setPluginEnabled: (key: string, enabled: boolean) =>
    getJSON<{ ok: boolean; plugin: PluginItem; error?: string }>(
      `/api/plugins/${encodeURIComponent(key)}/enabled`,
      { method: 'PUT', ...jsonBody({ enabled }) },
    ),
  platforms: () => getJSON<PlatformRow[]>('/api/platforms'),
  platformConfig: (name: string) =>
    getJSON<PlatformConfigResponse>(`/api/platforms/${encodeURIComponent(name)}/config`),
  savePlatformConfig: (name: string, payload: PlatformSavePayload) =>
    getJSON<PlatformConfigResponse & { saved: boolean }>(`/api/platforms/${encodeURIComponent(name)}/config`, {
      method: 'PUT',
      ...jsonBody(payload),
    }),
  connectPlatform: (name: string) =>
    getJSON<{ ok: boolean; status: PlatformRow; error?: string }>(`/api/platforms/${encodeURIComponent(name)}/connect`, {
      method: 'POST',
    }),
  disconnectPlatform: (name: string) =>
    getJSON<{ ok: boolean; status: PlatformRow; error?: string }>(`/api/platforms/${encodeURIComponent(name)}/disconnect`, {
      method: 'POST',
    }),
  reconnectPlatform: (name: string) =>
    getJSON<{ ok: boolean; status: PlatformRow; error?: string }>(`/api/platforms/${encodeURIComponent(name)}/reconnect`, {
      method: 'POST',
    }),
  qrLoginStart: (name: string) =>
    getJSON<{ ok: boolean; qr_id: string; qr_image: string; qrcode_url: string; error?: string }>(
      `/api/platforms/${encodeURIComponent(name)}/qr-login/start`,
      { method: 'POST' },
    ),
  qrLoginStatus: (name: string, qrId: string) =>
    getJSON<{ ok: boolean; status: string; account_id?: string; token?: string; error?: string }>(
      `/api/platforms/${encodeURIComponent(name)}/qr-login/status`,
      { method: 'POST', ...jsonBody({ qr_id: qrId }) },
    ),
  deletePlatformAccount: (name: string) =>
    getJSON<PlatformConfigResponse & { deleted: boolean; status: PlatformRow; error?: string }>(
      `/api/platforms/${encodeURIComponent(name)}/account`,
      { method: 'DELETE' },
    ),

  // ── MCP Server 管理（/api/mcp/servers，admin-only）──

  mcpServers: () =>
    getJSON<{ ok: boolean; servers: McpServerRow[] }>('/api/mcp/servers'),
  createMcpServer: (payload: McpServerPayload) =>
    getJSON<{ ok: boolean; servers: McpServerRow[] }>('/api/mcp/servers', {
      method: 'POST',
      ...jsonBody(payload),
    }),
  updateMcpServer: (name: string, payload: McpServerPayload) =>
    getJSON<{ ok: boolean; servers: McpServerRow[] }>(`/api/mcp/servers/${encodeURIComponent(name)}`, {
      method: 'PUT',
      ...jsonBody(payload),
    }),
  deleteMcpServer: (name: string) =>
    getJSON<{ ok: boolean; servers: McpServerRow[] }>(`/api/mcp/servers/${encodeURIComponent(name)}`, {
      method: 'DELETE',
    }),
  reloadMcpServer: (name: string) =>
    getJSON<{ ok: boolean; servers: McpServerRow[] }>(`/api/mcp/servers/${encodeURIComponent(name)}/reload`, {
      method: 'POST',
    }),

  // ── CUA Driver（Computer Use）一键安装（/api/mcp/cua-driver/*，登录用户可用）──

  cuaDriverStatus: () => getJSON<CuaDriverStatus>('/api/mcp/cua-driver/status'),
  cuaDriverSetup: (opts?: { force_reinstall?: boolean; start_daemon?: boolean }) =>
    getJSON<{ ok: boolean; task_id: string; status: string }>('/api/mcp/cua-driver/setup', {
      method: 'POST',
      ...jsonBody(opts ?? {}),
    }),
  cuaDriverSetupStatus: (taskId: string) =>
    getJSON<CuaSetupProgress & { ok: boolean }>(
      `/api/mcp/cua-driver/setup/${encodeURIComponent(taskId)}`,
    ),
  cuaDriverCancel: (taskId: string) =>
    getJSON<{ ok: boolean; task_id: string; status: string }>(
      `/api/mcp/cua-driver/setup/${encodeURIComponent(taskId)}/cancel`,
      { method: 'POST' },
    ),

  // ── LLM Wiki 知识库（/api/wiki/*，Phase 1 只读浏览 + Phase 2 上传/编译/删除）──

  /** Wiki Agent 专用会话：默认复用当前 KB 最近会话，也可显式新建。 */
  wikiAgentSession: (kbId?: string, opts?: { forceNew?: boolean }) =>
    getJSON<{ ok: boolean; session_id: string; kb_id: string }>(
      `${withKb('/api/wiki/agent-session', kbId)}${opts?.forceNew ? `${kbId ? '&' : '?'}force_new=true` : ''}`,
      { method: 'POST' },
    ),
  wikiAgentSessions: (kbId?: string) =>
    getJSON<{ ok: boolean; kb_id: string; sessions: WikiAgentSessionSummary[] }>(
      withKb('/api/wiki/agent-sessions', kbId),
    ),
  wikiCancelConfirmation: (confirmationId: string, sessionId: string) =>
    getJSON<{ ok: boolean; cancelled: boolean }>(
      `/api/wiki/confirmations/${encodeURIComponent(confirmationId)}/cancel`,
      { method: 'POST', ...jsonBody({ session_id: sessionId }) },
    ),
  wikiKBs: () => getJSON<{ ok: boolean; kbs: WikiKB[] }>('/api/wiki/kbs'),
  /** 初始化 KB（对齐 web WikiHub：无 KB 时自动初始化 default；后端幂等）。 */
  wikiInit: (kbId?: string) =>
    getJSON<{ ok: boolean }>(withKb('/api/wiki/init', kbId), { method: 'POST' }),
  /** 新建知识库（对齐 web WikiHub：kb_id 必填，name 缺省同 id）。 */
  wikiCreateKB: (payload: { kb_id: string; name?: string }) =>
    getJSON<{ ok: boolean; kb: WikiKB }>('/api/wiki/kbs', { method: 'POST', ...jsonBody(payload) }),
  /** 删除知识库及其全部页面（后端禁止删除 default）。 */
  wikiDeleteKB: (kbId: string) =>
    getJSON<{ ok: boolean }>(`/api/wiki/kbs/${encodeURIComponent(kbId)}`, { method: 'DELETE' }),
  wikiVaultDocument: (name: 'Home.md' | 'index.md', kbId?: string) =>
    getJSON<{ ok: boolean; document: WikiVaultDocument }>(
      withKb(`/api/wiki/vault-documents/${encodeURIComponent(name)}`, kbId),
    ),
  wikiPages: (params?: { limit?: number; offset?: number; kb_id?: string; brief?: boolean }) => {
    const p = new URLSearchParams();
    if (params?.limit !== undefined) p.set('limit', String(params.limit));
    if (params?.offset !== undefined) p.set('offset', String(params.offset));
    if (params?.kb_id) p.set('kb_id', params.kb_id);
    p.set('brief', params?.brief === false ? '0' : '1');
    return getJSON<{
      ok: boolean;
      pages: WikiPage[];
      source_titles: WikiSourceTitles;
      source_files: WikiSourceFiles;
    }>(`/api/wiki/pages?${p.toString()}`);
  },
  wikiPage: (id: string, kbId?: string) =>
    getJSON<{
      ok: boolean;
      page: WikiPage;
      source_titles: WikiSourceTitles;
      source_files: WikiSourceFiles;
      source_pages: WikiSourcePage[];
      relation_pages: WikiRelationPage[];
    }>(withKb(`/api/wiki/pages/${encodeURIComponent(id)}`, kbId)),
  wikiCreatePage: (
    payload: Pick<WikiPage, 'title' | 'content'> & Partial<Pick<WikiPage, 'page_type' | 'status'>>,
    kbId?: string,
  ) =>
    getJSON<{
      ok: boolean;
      page: WikiPage;
      source_titles: WikiSourceTitles;
      source_files: WikiSourceFiles;
    }>(withKb('/api/wiki/pages', kbId), { method: 'POST', ...jsonBody(payload) }),
  wikiUpdatePage: (
    id: string,
    payload: Partial<Pick<WikiPage, 'title' | 'content' | 'tags' | 'sources' | 'relations'>>,
    kbId?: string,
  ) =>
    getJSON<{
      ok: boolean;
      page: WikiPage;
      source_titles: WikiSourceTitles;
      source_files: WikiSourceFiles;
      source_pages: WikiSourcePage[];
      relation_pages: WikiRelationPage[];
    }>(withKb(`/api/wiki/pages/${encodeURIComponent(id)}`, kbId), {
      method: 'PUT',
      ...jsonBody(payload),
    }),
  wikiSearch: (query: string, kbId?: string, topK = 5) =>
    getJSON<{
      ok: boolean;
      pages: WikiPage[];
      source_titles: WikiSourceTitles;
      source_files: WikiSourceFiles;
    }>(withKb(`/api/wiki/search?q=${encodeURIComponent(query)}&top_k=${topK}`, kbId)),
  wikiSummary: (kbId?: string, force?: boolean) =>
    getJSON<{ ok: boolean } & WikiSummary>(withKb(`/api/wiki/summary${force ? '?force=true' : ''}`, kbId)),
  /** 知识图谱（Phase 3）：全量节点 + 关系边，不走分页。 */
  wikiGraph: (kbId?: string) =>
    getJSON<{ ok: boolean; graph: WikiGraph }>(withKb('/api/wiki/graph', kbId)),
  /** 上传本地文件（走主进程 gateway:upload IPC，一次一个文件）。 */
  wikiUpload: (filePath: string, kbId?: string) =>
    uploadJSON<WikiUploadResult>(withKb('/api/wiki/upload', kbId), filePath),
  /** 编译 source 为 Wiki 页面；传 sessionId 时后端经 WS 推送 wiki_ingest_progress。 */
  wikiIngest: (sourceId: string, kbId?: string, sessionId?: string) =>
    getJSON<{ ok: boolean; source_id: string; pages: WikiPage[]; issues: string[] }>(
      withKb('/api/wiki/ingest', kbId),
      { method: 'POST', ...jsonBody({ source_id: sourceId, session_id: sessionId ?? '' }) },
    ),
  wikiCancelIngest: (sourceId: string, kbId?: string) =>
    getJSON<{ ok: boolean; cancelled?: boolean }>(withKb('/api/wiki/ingest/cancel', kbId), {
      method: 'POST',
      ...jsonBody({ source_id: sourceId }),
    }),
  wikiDeletePage: (id: string, kbId?: string) =>
    getJSON<{ ok: boolean }>(withKb(`/api/wiki/pages/${encodeURIComponent(id)}`, kbId), { method: 'DELETE' }),
  wikiDeletePages: (ids: string[], kbId?: string) =>
    getJSON<{ ok: boolean; deleted: string[]; failed: Array<{ id: string; error: string }> }>(
      withKb('/api/wiki/pages', kbId),
      { method: 'DELETE', ...jsonBody({ page_ids: ids }) },
    ),
};

export type BackendSocketStatusMeta = {
  /** 主进程为换 socket 主动 close(1000, reconnect)，非真实断连。 */
  transient?: boolean;
};

export class BackendChatSocket {
  private ws: WebSocket | null = null;
  private closed = false;
  private usingGatewayProxy = false;
  private gatewayProxyOpen = false;
  private connectInFlight = false;
  private unsubscribeGatewayProxy: (() => void) | null = null;
  private reconnectTimer: number | null = null;
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
          logStream('ws-renderer', 'proxy-event-open', {});
          this.onStatus(true);
          this.resubscribe();
          this.onOpen?.();
          return;
        }
        if (payload.type === 'message') {
          const frame = JSON.parse(String(payload.data || '{}')) as ChatChunk;
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
          if (!this.closed && !transient) {
            this.reconnectTimer = window.setTimeout(() => this.connect(), 1500);
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
          if (!this.closed) {
            this.reconnectTimer = window.setTimeout(() => this.connect(), 1500);
          }
        }
      });
      return;
    }
    logStream('ws-renderer', 'connect-direct-ws', { wsBase: wsBase() });
    const wsUrl = `${wsBase()}/ws`;
    this.ws = new WebSocket(wsUrl);
    this.ws.onopen = () => {
      this.onStatus(true);
      this.resubscribe();
      this.onOpen?.();
    };
    this.ws.onmessage = (event) => {
      const payload = JSON.parse(event.data) as ChatChunk;
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
      if (!this.closed) {
        this.reconnectTimer = window.setTimeout(() => this.connect(), 1500);
      }
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
// Work 办公域客户端：复用既有 gatewayFetch / getJSON / jsonBody，不复制 fetch/WS。
// 端点与 crew/gateway/routers/work.py 对齐；类型与后端 asdict()/to_dict() 对齐。
// ---------------------------------------------------------------------------

export type WorkHistoryEntityType =
  | 'work_session'
  | 'work_item_session'
  | 'work_item'
  | 'agent_session';
export interface WorkHistoryEntry {
  id: string;
  entity_type: WorkHistoryEntityType;
  session_id: string | null;
  title: string;
  workspace_id: string | null;
  updated_at: number;
  work_item_id: string | null;
  archived: boolean;
  pinned: boolean;
  read_only: boolean;
  open_mode: 'work' | 'assistant';
}

export interface WorkSession {
  session_id: string;
  title: string;
  workspace_id: string;
  product_mode: 'work';
}

export interface WorkItem {
  item_id: string;
  owner_account_id: string;
  title: string;
  description?: string;
  category?: string | null;
  related_system?: string | null;
  workspace_id?: string | null;
  processing_session_id?: string | null;
  business_status?: string;
  execution_status?: string;
  sync_status?: string;
  priority?: string;
  disposition?: string;
  source?: {
    connector_key: string;
    external_id: string;
    external_version: string;
  } | null;
  due_at?: number | null;
  version: number;
  created_at: number;
  updated_at: number;
}

export interface WorkReference {
  reference_id: string;
  target_session_id: string;
  reference_type: string;
  source_id: string;
  target_item_id?: string | null;
  snapshot_version?: string;
  snapshot_summary?: string;
  source_link?: string;
  created_at: number;
  updated_at: number;
}

export interface WorkPreference {
  owner_account_id: string;
  preference_id: string;
  category: string;
  content: string;
  scope: 'global' | 'item_type' | 'workspace' | 'source';
  scope_id: string | null;
  status: 'active' | 'paused';
  auto_enabled: boolean;
  evidence_session_count: number;
  version: number;
  created_at: number;
  updated_at: number;
}

export interface WorkSourceState {
  owner_account_id: string;
  connector_key: string;
  enabled: boolean;
  status: 'disabled' | 'idle' | 'syncing' | 'ready' | 'error' | 'unavailable';
  cursor?: string | null;
  last_error?: string;
  last_synced_at?: number | null;
  updated_at: number;
}

export interface WorkSourceRecord {
  owner_account_id: string;
  record_id: string;
  connector_key: string;
  external_id: string;
  external_version: string;
  title: string;
  kind: string;
  source_status: string;
  due_at: number | null;
  source_url: string;
  normalized: Record<string, unknown>;
  pending_writeback: Record<string, unknown>;
  conflict_external: Record<string, unknown>;
  conflict_local: Record<string, unknown>;
  sync_status: string;
  updated_at: number;
}

export interface WorkKnowledgePage {
  id: string;
  page_type: string;
  title: string;
  content?: string;
  summary?: string | null;
  tags?: string[];
  sources?: unknown[];
  related?: unknown[];
  aliases?: string[];
  created_at?: number;
  updated_at?: number;
}

export interface WorkTemplate {
  owner_account_id: string;
  template_id: string;
  source: 'system' | 'organization' | 'personal';
  name: string;
  description: string;
  category: string;
  blueprint: Record<string, unknown>;
  version: number;
  usage_count: number;
  last_used_at?: number | null;
  created_at: number;
  updated_at: number;
}

export interface WorkDashboard {
  brief: {
    brief_id: string;
    business_date: string;
    workspace_id: string | null;
    content: Record<string, unknown>;
    version: number;
    archived: boolean;
    created_at: number;
    updated_at: number;
  } | null;
}

export interface WorkPeriodReport {
  report_id: string | null;
  period: 'day' | 'week' | 'month';
  period_start: string;
  period_end: string;
  workspace_id: string | null;
  metrics: {
    created: number;
    completed: number;
    in_progress: number;
    overdue: number;
    completion_rate: number;
    status_counts: Record<string, number>;
    category_counts: Record<string, number>;
  };
  archived: boolean;
  generated_at: number;
  archived_at: number | null;
}

export interface WorkIndexStatus {
  enabled: boolean;
  state: string;
  updated_at: number;
}
export interface WorkItemEvent {
  event_id: string;
  owner_account_id: string;
  item_id: string;
  event_type: string;
  actor: string;
  before_state?: Record<string, unknown> | null;
  after_state?: Record<string, unknown> | null;
  created_at: number;
}

export const workApi = {
  // 历史
  createSession: (payload: { workspace_id: string; title: string }) =>
    getJSON<WorkSession>('/api/work/sessions', { method: 'POST', ...jsonBody(payload) }),
  history: () => getJSON<{ entries: WorkHistoryEntry[]; count: number }>('/api/work/history'),
  // 事项
  listItems: (workspaceId?: string | null) => {
    const query = workspaceId ? `?workspace_id=${encodeURIComponent(workspaceId)}` : '';
    return getJSON<{ items: WorkItem[]; count: number }>(`/api/work/items${query}`);
  },
  createItem: (payload: { title: string; workspace_id?: string; [k: string]: unknown }) =>
    getJSON<WorkItem>('/api/work/items', { method: 'POST', ...jsonBody(payload) }),
  getItem: (itemId: string) => getJSON<WorkItem>(`/api/work/items/${encodeURIComponent(itemId)}`),
  updateItem: (itemId: string, payload: { expected_version: number; title?: string; [k: string]: unknown }) =>
    getJSON<WorkItem>(`/api/work/items/${encodeURIComponent(itemId)}`, { method: 'PATCH', ...jsonBody(payload) }),
  actOnItem: (itemId: string, payload: { action: string; expected_version: number; due_at?: number }) =>
    getJSON<WorkItem>(`/api/work/items/${encodeURIComponent(itemId)}/actions`, { method: 'POST', ...jsonBody(payload) }),
  startItemProcessingSession: (itemId: string, payload: { expected_version: number }) =>
    getJSON<WorkItem>(`/api/work/items/${encodeURIComponent(itemId)}/processing-session`, {
      method: 'POST',
      ...jsonBody(payload),
    }),
  getItemActivity: (itemId: string) =>
    getJSON<{ events: WorkItemEvent[]; count: number }>(`/api/work/items/${encodeURIComponent(itemId)}/activity`),
  saveItemKnowledge: (itemId: string, full = true) =>
    getJSON<{ page: WorkKnowledgePage }>(`/api/work/items/${encodeURIComponent(itemId)}/knowledge`, {
      method: 'POST',
      ...jsonBody({ full }),
    }),
  deleteItem: (itemId: string, payload: { expected_version: number; confirm: string }) =>
    getJSON<{ ok: boolean }>(`/api/work/items/${encodeURIComponent(itemId)}`, { method: 'DELETE', ...jsonBody(payload) }),
  // 引用
  listReferences: (targetSessionId: string) =>
    getJSON<{ items: WorkReference[]; count: number }>(`/api/work/references?target_session_id=${encodeURIComponent(targetSessionId)}`),
  createReference: (payload: { target_session_id: string; reference_type: string; source_id: string; source_link?: string }) =>
    getJSON<WorkReference>('/api/work/references', { method: 'POST', ...jsonBody(payload) }),
  deleteReference: (referenceId: string) =>
    getJSON<{ ok: boolean }>(`/api/work/references/${encodeURIComponent(referenceId)}`, { method: 'DELETE' }),
  refreshReference: (referenceId: string) =>
    getJSON<WorkReference>(`/api/work/references/${encodeURIComponent(referenceId)}/refresh`, { method: 'POST' }),
  // 偏好
  // @ 提及搜索（事项 / 会话 / 知识 / 来源记录）
  searchMentions: async (query: string, workspaceId?: string | null) => {
    const params = new URLSearchParams({ q: query });
    if (workspaceId) params.set('workspace_id', workspaceId);
    return (await getJSON<{ items: Array<{ entity_type: string; id: string; title: string; workspace_id?: string; source_link?: string }>; count: number }>(`/api/work/mentions?${params}`)).items;
  },
  createAgentSessionReference: (payload: { target_session_id: string; source_session_id: string }) =>
    getJSON<WorkReference>('/api/work/references/agent-session', { method: 'POST', ...jsonBody(payload) }),
  getPreferenceSettings: () => getJSON<{ auto_learning_enabled: boolean }>('/api/work/preferences/settings'),
  setPreferenceSettings: (enabled: boolean) =>
    getJSON<{ auto_learning_enabled: boolean }>('/api/work/preferences/settings', { method: 'PUT', ...jsonBody({ auto_learning_enabled: enabled }) }),
  listPreferences: () => getJSON<{ items: WorkPreference[]; count: number }>('/api/work/preferences'),
  createPreference: (payload: { category: string; content: string }) =>
    getJSON<WorkPreference>('/api/work/preferences', {
      method: 'POST',
      ...jsonBody(payload),
    }),
  updatePreference: (preferenceId: string, payload: {
    expected_version: number;
    content?: string;
    scope?: WorkPreference['scope'];
    scope_id?: string | null;
    status?: WorkPreference['status'];
  }) =>
    getJSON<WorkPreference>(`/api/work/preferences/${encodeURIComponent(preferenceId)}`, { method: 'PATCH', ...jsonBody(payload) }),
  deletePreference: (preferenceId: string, expectedVersion: number) =>
    getJSON<{ ok: boolean }>(`/api/work/preferences/${encodeURIComponent(preferenceId)}`, {
      method: 'DELETE',
      ...jsonBody({ expected_version: expectedVersion }),
    }),
  // 来源
  listSources: () => getJSON<{ items: WorkSourceState[]; count: number }>('/api/work/sources'),
  toggleSource: (connectorKey: string, enabled: boolean) =>
    getJSON<WorkSourceState>(`/api/work/sources/${encodeURIComponent(connectorKey)}`, { method: 'PUT', ...jsonBody({ enabled }) }),
  refreshSource: (connectorKey: string) =>
    getJSON<WorkSourceState>(`/api/work/sources/${encodeURIComponent(connectorKey)}/refresh`, { method: 'POST', ...jsonBody({}) }),
  deleteSourceLocalData: (connectorKey: string) =>
    getJSON<{ ok: boolean; deleted_records: number }>(`/api/work/sources/${encodeURIComponent(connectorKey)}/data`, {
      method: 'DELETE',
      ...jsonBody({ confirm: 'delete_work_source_local_data' }),
    }),
  listSourceRecords: (connectorKey?: string) => {
    const query = connectorKey ? `?connector_key=${encodeURIComponent(connectorKey)}` : '';
    return getJSON<{ items: WorkSourceRecord[]; count: number }>(`/api/work/sources/records${query}`);
  },
  resolveSourceConflict: (recordId: string, resolution: 'external' | 'local') =>
    getJSON<WorkSourceRecord>(`/api/work/sources/records/${encodeURIComponent(recordId)}/resolve`, {
      method: 'POST',
      ...jsonBody({ resolution }),
    }),
  // 看板
  getDashboard: (workspaceId?: string | null) => {
    const query = workspaceId ? `?workspace_id=${encodeURIComponent(workspaceId)}` : '';
    return getJSON<WorkDashboard>(`/api/work/dashboard${query}`);
  },
  refreshDashboard: (workspaceId?: string | null) =>
    getJSON<WorkDashboard>('/api/work/dashboard/refresh', {
      method: 'POST',
      ...jsonBody(workspaceId ? { workspace_id: workspaceId } : {}),
    }),
  archiveDashboard: (workspaceId?: string | null) =>
    getJSON<WorkDashboard>('/api/work/dashboard/archive', {
      method: 'POST',
      ...jsonBody(workspaceId ? { workspace_id: workspaceId } : {}),
    }),
  getReport: (
    period: WorkPeriodReport['period'],
    anchor: string,
    workspaceId?: string | null,
  ) => {
    const query = new URLSearchParams({ period, anchor });
    if (workspaceId) query.set('workspace_id', workspaceId);
    return getJSON<{ report: WorkPeriodReport }>(`/api/work/reports?${query}`);
  },
  archiveReport: (
    period: WorkPeriodReport['period'],
    anchor: string,
    workspaceId?: string | null,
  ) =>
    getJSON<{ report: WorkPeriodReport }>('/api/work/reports/archive', {
      method: 'POST',
      ...jsonBody({
        period,
        anchor,
        ...(workspaceId ? { workspace_id: workspaceId } : {}),
      }),
    }),
  // 设置
  getSettings: () => getJSON<Record<string, unknown>>('/api/work/settings'),
  putSettings: (payload: Record<string, unknown>) =>
    getJSON<Record<string, unknown>>('/api/work/settings', { method: 'PUT', ...jsonBody(payload) }),
  // 模板
  listTemplates: () => getJSON<{ items: WorkTemplate[]; count: number }>('/api/work/templates'),
  createTemplate: (payload: { name: string; description?: string; category?: string; blueprint?: Record<string, unknown> }) =>
    getJSON<WorkTemplate>('/api/work/templates', { method: 'POST', ...jsonBody(payload) }),
  updateTemplate: (templateId: string, payload: { name?: string; description?: string; category?: string; blueprint?: Record<string, unknown> }) =>
    getJSON<WorkTemplate>(`/api/work/templates/${encodeURIComponent(templateId)}`, { method: 'PATCH', ...jsonBody(payload) }),
  deleteTemplate: (templateId: string) =>
    getJSON<{ ok: boolean }>(`/api/work/templates/${encodeURIComponent(templateId)}`, { method: 'DELETE', ...jsonBody({}) }),
  instantiateTemplate: (templateId: string, payload: Record<string, unknown>) =>
    getJSON<WorkItem>(`/api/work/templates/${encodeURIComponent(templateId)}/instantiate`, { method: 'POST', ...jsonBody(payload) }),
  // 知识
  listPersonalKnowledge: () => getJSON<{ items: WorkKnowledgePage[]; count: number }>('/api/work/knowledge/personal'),
  savePersonalKnowledge: (payload: { title: string; content: string }) =>
    getJSON<{ page: WorkKnowledgePage }>('/api/work/knowledge/personal', { method: 'POST', ...jsonBody(payload) }),
  listOrganizationKnowledge: () => getJSON<{
    items: WorkKnowledgePage[];
    count: number;
    available: boolean;
  }>('/api/work/knowledge/organization'),
  requestPublish: (payload: { page_id: string; target: string }) =>
    getJSON<{ request_id: string; status: string }>('/api/work/knowledge/publish', { method: 'POST', ...jsonBody(payload) }),
  listPublishRequests: () => getJSON<{ items: Record<string, unknown>[]; count: number }>('/api/work/knowledge/publish'),
  // Workspace 索引状态
  getIndexStatus: (workspaceId: string) =>
    getJSON<WorkIndexStatus>(`/api/work/workspaces/${encodeURIComponent(workspaceId)}/index`),
  setIndexStatus: (workspaceId: string, payload: { enabled?: boolean; state?: string }) =>
    getJSON<WorkIndexStatus>(`/api/work/workspaces/${encodeURIComponent(workspaceId)}/index`, { method: 'PUT', ...jsonBody(payload) }),
  deleteIndexStatus: (workspaceId: string) =>
    getJSON<{ ok: boolean }>(`/api/work/workspaces/${encodeURIComponent(workspaceId)}/index`, { method: 'DELETE' }),
};

// ---------------------------------------------------------------------------
// 办公系统客户端（邮件 / 待办 / 日程 / 会议）：复用 gatewayFetch / getJSON / jsonBody，不复制 fetch。
// 端点与 crew/api/{mail,todo,schedule,meeting}_router.py 对齐。四个 GET /latest 是后台
// MailTodoRefresher 的只读内存快照（不触发外部调用）；其余为实时调用。响应业务失败为 200 壳
// {ok:false,error,code}，由调用方按 ok 判定。
// ---------------------------------------------------------------------------

/** 后台定时刷新的只读快照形态（四个 /latest 通用）。data=null 表示首轮未完成/账号切换/开关关闭。 */
export interface OfficeSnapshot<T> {
  ok: boolean;
  data: T | null;
  fetched_at: number | null;
  stale: boolean;
  error: string | null;
}

// ── 邮件（139 邮箱）──
/** /api/mail/search 与 /api/mail/latest 的单封邮件摘要。mid 为内部标识，用于详情/转发入参。 */
export interface MailMessage {
  subject: string;
  from: string;
  sendDate?: string;
  summary?: string;
  mid: string;
  read?: boolean;
  readStatus?: string;
  /** 可选的详情跳转地址。 */
  detail_link?: string;
  [k: string]: unknown;
}
export interface MailSearchData {
  count: number;
  results: MailMessage[];
}
export interface MailSearchResponse {
  ok: boolean;
  count: number;
  results: MailMessage[];
  error?: string;
  code?: string;
}
export interface MailDetailResponse {
  ok: boolean;
  subject: string;
  content: string;
  from: string;
  error?: string;
  code?: string;
}
export interface MailSendResponse {
  ok: boolean;
  message?: string;
  attachment_names?: string[];
  cc?: string;
  error?: string;
  code?: string;
}
export interface MailForwardResponse {
  ok: boolean;
  message?: string;
  subject?: string;
  error?: string;
  code?: string;
}
export interface OfficeMailCompose {
  to: string;
  subject: string;
  content: string;
  cc?: string;
  attachments?: string[];
}

// ── 待办（总部待办）──
/** 待办 dataList 元素为上游原始 ViewEntry，含跳转链接 url。 */
export interface TodoItem {
  itemTitle: string;
  systemName?: string;
  drafterName?: string;
  itemCreateTime?: string;
  url?: string;
  [k: string]: unknown;
}
export interface TodoGroup {
  groupName: string;
  count: number;
  dataList: TodoItem[];
  [k: string]: unknown;
}
export interface TodoData {
  summary?: string;
  groups: TodoGroup[];
  counts?: unknown[];
}
export interface TodoFetchResponse extends TodoData {
  ok: boolean;
  error?: string;
  code?: string;
}
export interface TodoCategoriesResponse {
  ok: boolean;
  categories: unknown[];
  error?: string;
  code?: string;
}

// ── 日程（企业日程）──
/** 日程 results 元素为上游原始字段，含 scheduleId（改/删时的 schdule_id）。 */
export interface ScheduleItem {
  scheduleId: string;
  scheduleTheme?: string;
  scheduleStartDate?: string;
  scheduleStartTime?: string;
  scheduleEndTime?: string;
  scheduleEndDate?: string;
  [k: string]: unknown;
}
export interface ScheduleData {
  total: number;
  pages: number;
  count: number;
  results: ScheduleItem[];
}
export interface ScheduleSearchResponse extends ScheduleData {
  ok: boolean;
  error?: string;
  code?: string;
}
export interface ScheduleSyncResponse {
  ok: boolean;
  schduleId?: string;
  error?: string;
  code?: string;
}
export interface OfficeScheduleSync {
  operate_id: 0 | 1 | 2;
  theme: string;
  start_date: string;
  start_time: string;
  end_date: string;
  end_time: string;
  remind_mode: number;
  schdule_id?: string;
  remark?: string;
  schedule_priority?: number;
  meeting_no?: string;
  meeting_code?: string;
  meeting_place?: string;
  user_ids?: string[];
  presenter?: string;
  related_url?: string;
  back_url_pc?: string;
}

// ── 会议（智慧会议待参会议）──
/** status: 1 已发布 / 2 进行中 / 3 暂停；详情跳转地址由服务端提供。 */
export interface MeetingItem {
  infoId: number;
  infoName: string;
  status: number;
  time: string;
  conferenceTypeName?: string;
  url?: string;
  [k: string]: unknown;
}
export interface MeetingData {
  wait_count: number;
  meetings: MeetingItem[];
}
export interface MeetingPendingResponse extends MeetingData {
  ok: boolean;
  error?: string;
  code?: string;
}

export const officeApi = {
  // 邮件
  mailLatest: () => getJSON<OfficeSnapshot<MailSearchData>>('/api/mail/latest'),
  mailSearch: (payload: {
    search_subject?: string;
    search_from?: string;
    search_content?: string;
    read_status?: 0 | 1;
    channel?: string;
  }) => getJSON<MailSearchResponse>('/api/mail/search', { method: 'POST', ...jsonBody(payload) }),
  mailDetail: (mid: string) =>
    getJSON<MailDetailResponse>('/api/mail/detail', { method: 'POST', ...jsonBody({ mid }) }),
  mailSend: (payload: OfficeMailCompose) =>
    getJSON<MailSendResponse>('/api/mail/send', { method: 'POST', ...jsonBody(payload) }),
  mailForward: (mid: string, to: string) =>
    getJSON<MailForwardResponse>('/api/mail/forward', { method: 'POST', ...jsonBody({ mid, to }) }),
  // 待办
  todoLatest: () => getJSON<OfficeSnapshot<TodoData>>('/api/todo/latest'),
  todoFetch: (payload: {
    type: 'DB' | 'DY';
    url_type?: 'HOME' | 'MORE';
    fetch_group_id?: string[];
    my_assistant_enum?: 'ALL' | 'URGENT' | 'OVERDUE';
  }) => getJSON<TodoFetchResponse>('/api/todo/fetch', { method: 'POST', ...jsonBody(payload) }),
  todoCategories: (fetch_data_type: 'DB' | 'DY' = 'DB') =>
    getJSON<TodoCategoriesResponse>('/api/todo/categories', {
      method: 'POST',
      ...jsonBody({ fetch_data_type }),
    }),
  // 日程
  scheduleLatest: () => getJSON<OfficeSnapshot<ScheduleData>>('/api/schedule/latest'),
  scheduleSearch: (payload: {
    start_time: string;
    end_time: string;
    page_num?: number;
    page_size?: number;
    theme?: string;
  }) => getJSON<ScheduleSearchResponse>('/api/schedule/search', { method: 'POST', ...jsonBody(payload) }),
  scheduleSync: (payload: OfficeScheduleSync) =>
    getJSON<ScheduleSyncResponse>('/api/schedule/sync', { method: 'POST', ...jsonBody(payload) }),
  // 会议
  meetingLatest: () => getJSON<OfficeSnapshot<MeetingData>>('/api/meeting/latest'),
  meetingPending: () => getJSON<MeetingPendingResponse>('/api/meeting/pending'),
};
