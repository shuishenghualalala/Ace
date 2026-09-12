/**
 * 后端 HTTP 客户端：gateway 后端（FastAPI /api/...）的类型化封装。
 *
 * 本文件为聚合门面：从 api/* 各 Feature 模块导入类型与 API 片段，
 * 组装为与拆分前完全一致的 backendApi / workApi / officeApi / notificationApi
 * 及 BackendChatSocket，并全量 re-export 公开类型。
 *
 * backendApi 覆盖的端点示例（保留文本引用供源码巡检）：
 * /api/session/*, /api/sites/inspirations, /api/wiki/*, /api/work/* 等。
 */
import { sessionApi, BackendChatSocket } from './api/feature-session';
import { systemApi } from './api/feature-system';
import { toolsApi } from './api/feature-tools';
import { wikiApi } from './api/feature-wiki';
import { workApi, officeApi, notificationApi } from './api/feature-work';

export const backendApi = {
  ...sessionApi,
  ...systemApi,
  ...toolsApi,
  ...wikiApi,
};

export { workApi, officeApi, notificationApi, BackendChatSocket };

// ── 共享传输层类型 ──
export type {
  Mode,
  ChunkKind,
  Attachment,
  SendPayload,
  ChatChunk,
  GatewayUploadFileResult,
  GatewayUploadResult,
  BackendSocketStatusMeta,
} from './api/transport';

// ── 会话 / Agent / Team / Cron / Browser / Dynamic Kanban / 追问 类型 ──
export type {
  SessionAgentConfig,
  SessionAgentBindingKind,
  SessionAgentBinding,
  BackendSession,
  BackendHistoryFileChange,
  BackendHistoryItem,
  TeamArtifactCard,
  SessionPlanState,
  ExternalRuntime,
  RuntimeModelProfile,
  TeamMemberModelBinding,
  SessionModelBindingResponse,
  AgentProfile,
  ExternalAgent,
  ExternalTeamMember,
  ExternalTeam,
  ExternalTeamSuggestionMember,
  RequiredAgentConflict,
  FormationPlanMember,
  FormationPlan,
  ExternalTeamSuggestion,
  FormationStaffingGap,
  ExternalTeamDraft,
  ExternalTeamDraftMeta,
  ExternalTeamDraftStreamOptions,
  ExternalTeamSuggestionStreamOptions,
  ExternalTeamRole,
  BrowserPageState,
  Task,
  CronJob,
  CronJobList,
  CronJobRun,
  CronJobDetail,
  CronDeliveryTarget,
  WorkflowPhase,
  DynamicKanbanStatus,
  FollowupQuestion,
  FollowupAnswer,
} from './api/feature-session';

// ── 系统 / 配置 / Plugins / Platforms / Sites / Skills 类型 ──
export type {
  ModelOption,
  ModelPayload,
  VendorModelOption,
  VendorProfileOption,
  BackendConfig,
  PluginItem,
  PlatformRow,
  PlatformConfigResponse,
  PlatformSavePayload,
  SystemMetrics,
  LogEntry,
  Workspace,
  LocalSite,
  SiteAnnotation,
  InspirationItem,
  InspirationAnnotation,
  InspirationDetail,
  InspirationSurface,
  BlueprintLayout,
  CanvasPlacement,
  BlueprintCanvas,
  BlueprintWidget,
  Skill,
  OptionalSkill,
  EvolutionConfig,
  SkillStore,
  CompleteItem,
} from './api/feature-system';

// ── 工具 / MCP / CUA 类型 ──
export type {
  ToolInfo,
  SubScenario,
  Scenario,
  McpTransport,
  McpServerConfig,
  McpServerRow,
  McpServerPayload,
  CuaSetupStep,
  CuaSetupProgress,
  CuaDriverStatus,
} from './api/feature-tools';

// ── Wiki 类型 ──
export type {
  WikiPageStatus,
  WikiPageType,
  WikiConfidence,
  WikiSourceType,
  WikiParseStatus,
  WikiViewMode,
  WikiKB,
  WikiPage,
  WikiRelation,
  WikiEvidence,
  WikiClaim,
  WikiSource,
  WikiSourceTitles,
  WikiRelationPage,
  WikiSourcePage,
  WikiSourceFiles,
  WikiVaultDocument,
  WikiGraph,
  WikiGraphNode,
  WikiGraphEdge,
  WikiIngestProgress,
  WikiUploadResult,
  WikiAgentSessionSummary,
} from './api/feature-wiki';

// ── Work / Office / Notification 类型 ──
export type {
  WorkHistoryEntityType,
  WorkHistoryEntry,
  WorkSession,
  WorkItem,
  WorkReference,
  WorkPreference,
  WorkSourceState,
  WorkSourceRecord,
  WorkKnowledgePage,
  WorkTemplate,
  WorkDashboard,
  WorkPeriodReport,
  WorkIndexStatus,
  WorkItemEvent,
  OfficeSnapshot,
  MailMessage,
  MailSearchData,
  MailSearchResponse,
  MailDetailResponse,
  MailSendResponse,
  MailForwardResponse,
  OfficeMailCompose,
  TodoItem,
  TodoGroup,
  TodoData,
  TodoFetchResponse,
  TodoCategoriesResponse,
  ScheduleItem,
  ScheduleData,
  ScheduleSearchResponse,
  ScheduleSyncResponse,
  OfficeScheduleSync,
  MeetingItem,
  MeetingData,
  MeetingPendingResponse,
  BackendNotification,
} from './api/feature-work';
