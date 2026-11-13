/**
 * Work 办公域客户端：Work 事项 / 办公系统（邮件、待办、日程、会议）/ 通知中心。
 * 复用共享传输层 gatewayFetch / getJSON / jsonBody，不复制 fetch/WS。
 */
import { getJSON, jsonBody } from './transport';

// ---------------------------------------------------------------------------
// Work 办公域客户端类型
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

// ---------------------------------------------------------------------------
// 办公系统客户端类型（邮件 / 待办 / 日程 / 会议）
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

// ---------------------------------------------------------------------------
// 通知中心类型
// ---------------------------------------------------------------------------

/** 通知中心单条通知。payload 为跳转上下文（如 {"session_id": "..."}），可为 null。 */
export interface BackendNotification {
  id: string;
  source: string;
  kind: string;
  title: string;
  body: string;
  payload: Record<string, unknown> | null;
  created_at: number;
  read_at: number | null;
}

// ---------------------------------------------------------------------------
// Work API
// ---------------------------------------------------------------------------

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
  createReference: (payload: { target_session_id: string; reference_type: string; source_id: string; source_link?: string; snapshot_summary?: string }) =>
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
// 办公系统客户端（邮件 / 待办 / 日程 / 会议）
// ---------------------------------------------------------------------------

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

// ---------------------------------------------------------------------------
// 通知中心客户端
// ---------------------------------------------------------------------------

export const notificationApi = {
  list: (opts?: { limit?: number; offset?: number; unreadOnly?: boolean }) => {
    const params = new URLSearchParams();
    params.set('limit', String(opts?.limit ?? 50));
    params.set('offset', String(opts?.offset ?? 0));
    params.set('unread_only', String(opts?.unreadOnly ?? false));
    return getJSON<{ notifications: BackendNotification[]; unread_count: number }>(
      `/api/notifications?${params.toString()}`,
    );
  },
  markRead: (id: string) =>
    getJSON<{ ok: boolean }>(`/api/notifications/${encodeURIComponent(id)}/read`, { method: 'POST' }),
  markAllRead: () => getJSON<{ ok: boolean }>('/api/notifications/read-all', { method: 'POST' }),
  clear: () => getJSON<{ ok: boolean }>('/api/notifications', { method: 'DELETE' }),
};
