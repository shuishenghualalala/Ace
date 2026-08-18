export type Mode = "agent" | "team";
export type TeamExecutionTier = "auto" | "fast" | "standard" | "ai";

export interface Skill {
  name: string;
  display_name?: string;
  slug: string;
  aliases?: string[];
  description: string;
  description_zh?: string;
  query_examples?: string[];
  category: string;
  featured: boolean;
  source: "builtin" | "user";
}

export interface OptionalSkill {
  name: string;
  display_name?: string;
  slug: string;
  aliases?: string[];
  description: string;
  description_zh?: string;
  query_examples?: string[];
  category: string;
  source: "optional" | "local";
}

export interface SkillStore {
  installed: Skill[];
  optional: OptionalSkill[];
  /** ~/.agents/skills 中未安装的本地 skill（跨 agent 共享，软链安装）。 */
  local?: OptionalSkill[];
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

export type CrewIntroLine = string;
export type CrewLoadingStatus = string;

export type ChunkKind =
  | "delta"
  | "tool"
  | "task"
  | "status"
  | "final"
  | "error"
  | "thinking"
  | "plan_review"
  | "followup_question"
  | "todo_updated"
  | "todo_reminder"
  | "file_changes"
  | "workflow_progress"
  | "wiki_cards"
  | "wiki_ingest_progress"
  | "team_internal";

export interface TodoItem {
  id: string;
  content: string;
  status: "pending" | "in_progress" | "completed" | "cancelled" | string;
}

/** Plan 模式：模型调了 exit_plan_mode 后推送的待审/空计划内容。
 *  - empty=false（status pending/editing/readonly）：计划已落盘，弹审批卡。
 *  - empty=true（status "empty"）：计划文件为空，弹提示卡（无审批按钮），倒逼模型先写文件。 */
export interface PlanReview {
  plan: string;
  planFile: string;
  status?: "pending" | "editing" | "readonly" | "empty" | "approved" | "revising" | "rejected" | "cancelled";
  empty?: boolean;
  phase?: string;
}

export interface FollowupQuestion {
  question_id: string;
  title: string;
  record_history?: boolean;
  status?: "pending" | "expired" | "cancelled" | "resolved" | string;
  note?: string;
  origin?: {
    type: "acp" | string;
    agent_id?: string;
    agent_name?: string;
    origin_session_id?: string;
  };
  questions: {
    id: string;
    question: string;
    options: { label: string; value: string; description?: string }[];
    inputMode?: "choice" | "text" | string;
    allowFreeText?: boolean;
    multiSelect: boolean;
  }[];
}

export interface ToolCallInfo {
  toolCallId: string;
  name: string;
  uiLabel?: string;
  args?: string;
  result?: string;
  status: "generating" | "running" | "done" | "error";
  startedAt: number;
  duration?: number;
}

export interface Chunk {
  kind: ChunkKind;
  body: Record<string, any>;
  /** Gateway frames carry the originating request identity at the top level. */
  request_id?: string;
  is_final: boolean;
  sequence: number;
  /** Gateway 内部单调序列号，用于断线重连后回放定位 */
  gateway_sequence?: number;
  /** 该帧所属会话；前端据此把帧路由到对应会话的消息缓存 */
  session_id?: string;
}

export interface Session {
  session_id: string;
  title: string;
  message_count: number;
  updated_at: number;
  created_at?: number;
  workspace_id: string;
  agent_label?: {
    name: string;
    provider: string;
    display_badge?: string;
  };
  agent_binding?: {
    kind: "builtin" | "client" | "external_agent" | "external_team";
    id: string;
  };
}

export interface Workspace {
  id: string;
  name: string;
  description: string;
  instructions: string;
}

export interface Task {
  id: string;
  task_id?: string;
  kind?: "shell" | "subagent" | "agent_turn" | "team";
  session_id?: string;
  assignee: string | null;
  title: string;
  detail?: string;
  status: string;
  result: string;
  error?: string;
  progress?: Record<string, unknown>;
  output_ref?: string;
  created_at?: number | null;
  updated_at?: number | null;
  started_at?: number | null;
  finished_at?: number | null;
  last_activity_at?: number | null;
  backgrounded?: boolean;
  auto_backgrounded?: boolean;
}

export interface RuntimeSessionStatus {
  session_id: string;
  live: "idle" | "running" | "queued" | string;
  queue_depth: number;
  waiting_for_global_slot?: number;
  global_active?: number;
  global_queued?: number;
  max_active_runs?: number;
  queue_limit?: number;
  last_status?: string;
  last_error?: string;
}

export interface ActiveChild {
  child_id: string;
  parent_session_id: string;
  session_id: string;
  member: string;
  task_id: string;
  instruction: string;
  started_at: number;
}

export interface RuntimeConcurrency {
  max_active_runs: number;
  global_active: number;
  global_queued: number;
  sessions: Record<string, RuntimeSessionStatus>;
  active_children: Record<string, ActiveChild[]> | ActiveChild[];
}

export interface ModelOption {
  id: string;
  name: string;
  model: string;
  base_url: string;
  has_key: boolean;
  temperature: number;
  max_tokens: number | null;
  context_window: number | null;
  timeout: number;
}

export interface AppConfig {
  model: string;
  has_key: boolean;
  base_url: string;
  active_model_id: string;
  models: ModelOption[];
  wiki: WikiConfig;
  external_agents?: {
    enabled: boolean;
  };
}

export type WikiPageType = "entity" | "topic" | "source" | "comparison" | "synthesis";
export type WikiSourceType = "upload" | "url" | "session" | "paste" | "image" | "video";
export type WikiParseStatus = "pending" | "parsed" | "failed";
export type WikiViewMode = "timeline" | "tree" | "type" | "graph";

export interface WikiConfig {
  enabled: boolean;
}

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
  tags: string[];
  created_at: number;
  updated_at: number;
  aliases: string[];
  relations?: WikiRelation[];
}

export interface WikiRelation {
  target_page_id: string;
  relation: string;
}

export interface WikiRelationPage {
  id: string;
  title: string;
  page_type: WikiPageType;
  relation: string;
  direction: "outgoing" | "incoming";
}

export interface WikiSourcePage {
  id: string;
  title: string;
  page_type: WikiPageType;
}

export interface WikiVaultDocument {
  name: "Home.md" | "index.md";
  path: string;
  content: string;
  updated_at: number;
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

/** source_id -> 原始文件元信息 的映射 */
export type WikiSourceFiles = Record<string, { original_path: string; file_type?: string; title?: string }>;

export interface WikiGraph {
  nodes: WikiGraphNode[];
  edges: WikiGraphEdge[];
}

export interface WikiGraphNode {
  id: string;
  title: string;
  type: WikiPageType | "source";
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
  detail?: Record<string, any>;
}

export interface WikiUploadResult {
  ok: boolean;
  source_id: string;
  title: string;
  source_type?: "upload" | "image" | "video";
  ingested?: boolean;
  needs_confirmation?: boolean;
  pages?: WikiPage[];
  issues?: string[];
}

export interface WikiSummary {
  summary: string;
  kb_id: string;
  page_count?: number;
  source_count?: number;
  generated_at?: number;
  status: "ready" | "generating" | "empty" | "stale";
}

export interface ExternalRuntime {
  id: string;
  provider: string;
  name: string;
  display_badge?: string;
  executable_path: string;
  version: string;
  protocol: string;
  available?: boolean;
  availability_status?: "ready" | "degraded" | "unavailable";
  metadata?: Record<string, unknown>;
  created_at?: string;
  updated_at?: string;
  last_seen_at?: string;
}

export interface RuntimeModelProfile {
  id: string;
  label: string;
  provider?: string;
  default?: boolean;
  capabilities?: string[];
  thinking_levels?: string[];
}

export interface ExternalAgent {
  id: string;
  name: string;
  provider: string;
  display_badge?: string;
  runtime_id: string;
  model: string;
  system_prompt: string;
  custom_args: string[];
  custom_env: Record<string, string>;
  profile?: AgentProfile;
  profile_version?: number;
  profile_updated_at?: string | null;
  created_at: string;
  updated_at: string;
}

export interface ExternalTeamMember {
  id: string;
  team_id: string;
  agent_id: string;
  role: string;
  role_key?: string;
  role_label?: string;
  capabilities?: string[];
  assigned_capabilities?: string[];
  workflow_lane?: string;
  sort_order: number;
  created_at: string;
  agent_name?: string;
  agent_provider?: string;
  display_badge?: string;
}

export interface ExternalTeam {
  id: string;
  name: string;
  display_badge?: string;
  description: string;
  leader_agent_id: string;
  instructions: string;
  team_spec?: Record<string, unknown>;
  formation_plan?: FormationPlan;
  archived_at?: string | null;
  created_at: string;
  updated_at: string;
  members: ExternalTeamMember[];
}

export interface ExternalTeamSuggestion {
  leader_agent_id: string;
  workflow: string;
  members: ExternalTeamSuggestionMember[];
  requested_formation_mode: "fast" | "ai" | "auto";
  selected_formation_mode: "fast" | "ai";
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

export interface RequiredAgentConflict {
  agent_id: string;
  agent_name: string;
  required_capabilities: string[];
  matched_capabilities: string[];
  best_score: number;
  best_confidence?: number;
  reason: string;
}

export interface CapabilityEvidence {
  source: string;
  value: string;
  weight: number;
}

export interface CapabilityAssessment {
  score: number;
  confidence: number;
  evidence: CapabilityEvidence[];
}

export interface AgentProfile {
  version: number;
  agent_id: string;
  availability: string;
  runtime: string;
  model?: {
    id: string;
    label: string;
    binding_status: "valid" | "missing" | "unverified";
    capabilities: string[];
    thinking_levels: string[];
  };
  capabilities: Record<string, CapabilityAssessment>;
}

export interface FormationPlanMember {
  agent_id: string;
  role_key: string;
  role_label: string;
  assigned_capabilities: string[];
  responsibility: Record<string, unknown>;
  responsibility_markdown: string;
  selection_source: "user" | "system" | string;
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

export interface ExternalTeamDraftSlot {
  slot_id: string;
  role_key: string;
  role_label: string;
  capability?: string;
  agent_id: string;
  suggested_agent_id?: string;
  is_leader: boolean;
  required: boolean;
  locked: boolean;
  changed?: boolean;
}

export interface ExternalTeamDraft {
  description: string;
  workflow: string;
  slots: ExternalTeamDraftSlot[];
  team_spec?: Record<string, unknown>;
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

export interface ExternalTeamRole {
  key: string;
  label: string;
  description: string;
  capabilities: string[];
  workflow_lane: string;
}

export interface DebugEvent {
  ts: number;
  dir: string;
  session_id?: string;
  model?: string;
  messages?: unknown[];
  tools?: unknown[];
  tool_calls?: unknown[];
  text?: string;
  content?: string;
  error?: string;
  name?: string;
  [key: string]: unknown;
}

export type MsgRole = "user" | "assistant" | "tool" | "status" | "error" | "team_internal";

/** 附件类型 */
export interface Attachment {
  id: string;
  name: string;
  path?: string;
  type: "file" | "image" | "url";
  content?: string;
  previewUrl?: string;
}

export interface TeamArtifactCard {
  id?: string;
  artifact_id?: string;
  title: string;
  summary?: string;
  path?: string;
  content_type?: string;
  mime_type?: string;
  kind?: "document" | "spreadsheet" | "presentation" | "image" | "html" | "text" | "data" | string;
}

export interface TurnFileChangeSummary {
  path: string;
  name: string;
  added: number;
  removed: number;
  status: "added" | "modified" | "deleted";
  binary?: boolean;
}

export interface UiMessage {
  id: string;
  role: MsgRole;
  text: string;
  /** 思考过程（assistant 消息） */
  thinking?: string;
  /** 关联的工具调用（assistant 消息旁展示） */
  toolCalls?: ToolCallInfo[];
  sourceSessionId?: string;
  agentId?: string;
  agentName?: string;
  agentRole?: string;
  agentTone?: number;
  isLeader?: boolean;
  eventType?: string;
  nodeId?: string;
  mentionFrom?: string;
  mentionTo?: string[];
  mentionIntent?: string;
  communicationKind?: string;
  communicationStatus?: string;
  requestId?: string;
  replyTo?: string;
  communicationRequestText?: string;
  displayMode?: "chat" | "collapsible" | string;
  collapsedTitle?: string;
  processText?: string;
  artifacts?: TeamArtifactCard[];
  /** Files added, modified or deleted by this exact Agent turn. */
  turnFileChanges?: TurnFileChangeSummary[];
  /** 用户消息携带的附件（图像预览等） */
  attachments?: Attachment[];
  timestamp?: number;
  turnStartedAt?: number;
  turnDurationMs?: number;
  /** 本消息回合内产生的 plan 审批卡片。 */
  planReview?: PlanReview;
  /** 本消息回合内最新 todo 快照。 */
  todoSnapshot?: TodoItem[];
  /** 本消息回合内产生的 Wiki 页面卡片。 */
  wikiCards?: WikiPage[];
}

export interface TeamMemberView {
  agentId?: string;
  name: string;
  displayBadge?: string;
  role: string;
  isLeader?: boolean;
  tone?: number;
}

/** 用户在 Team Composer 中通过 @ 选择的成员，发送时随 WebSocket 请求传递。 */
export interface UserAgentMention {
  kind: "team_member";
  member_id: string;
}

/** 前端本地待发送队列项 */
export interface PendingMessage {
  id: string;
  query: string;
  attachments: Attachment[];
  mode: Mode;
  workspaceId: string;
  planActive?: boolean;
  subScenario?: string;
  externalTeamId?: string;
  wikiKbId?: string;
  teamExecutionTier?: TeamExecutionTier;
  userMentions?: UserAgentMention[];
}
