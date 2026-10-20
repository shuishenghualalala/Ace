/**
 * 系统/配置客户端：config/vendors/model、plugins、platforms、system metrics/logs、
 * sites/inspirations/canvases、skills、upload/complete。
 */
import { getJSON, jsonBody } from './transport';

// ── config / model / vendors ──

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

/** 后端厂商目录（GET /api/config/vendors）的公开视图。 */
export interface VendorModelOption {
  id: string;
  context_window?: number | null;
  max_tokens?: number | null;
  reasoning?: boolean;
  vision?: boolean;
}

export interface VendorProfileOption {
  id: string;
  name: string;
  protocol: 'openai' | 'anthropic';
  base_url: string;
  api_key_env: string;
  models: VendorModelOption[];
}

export interface BackendConfig {
  model: string;
  has_key: boolean;
  base_url: string;
  active_model_id: string;
  models: ModelOption[];
  model_profiles?: ModelOption[];
  is_gateway_admin?: boolean;
  wiki?: {
    enabled?: boolean;
  };
  external_agents?: {
    enabled?: boolean;
  };
  feature_capabilities?: Record<string, FeatureCapability>;
  security?: {
    enabled?: boolean;
    default_mode?: 'request_approval' | 'auto_review' | 'full_access';
  };
}

export interface FeatureCapability {
  state: string;
  available: boolean;
  generation: string | null;
}

// ── plugins / platforms ──

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

// ── system metrics / logs ──

/** /api/system/metrics 返回的宿主机 + 进程资源指标。 */
export interface SystemMetrics {
  uptime_s: number;
  cpu_count: number;
  cpu_percent?: number;
  memory?: { total_gb: number; used_gb: number; available_gb: number; percent: number };
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

// ── sites / inspirations / canvases / widgets ──

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

// ── skills ──

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

// ── misc ──

export interface CompleteItem {
  text: string;
  display: string;
  meta: string;
  type: string;
}

export const systemApi = {
  // config / model / vendors
  config: () => getJSON<BackendConfig>('/api/config'),
  configVendors: () => getJSON<{ vendors: VendorProfileOption[] }>('/api/config/vendors'),
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

  // plugins
  plugins: () => getJSON<PluginItem[]>('/api/plugins'),
  setPluginEnabled: (key: string, enabled: boolean) =>
    getJSON<{ ok: boolean; plugin: PluginItem; error?: string }>(
      `/api/plugins/${encodeURIComponent(key)}/enabled`,
      { method: 'PUT', ...jsonBody({ enabled }) },
    ),

  // platforms
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

  // inspirations / sites / canvases / widgets
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

  // workspaces
  workspaces: () => getJSON<Workspace[]>('/api/workspaces'),
  createWorkspace: (fields: Partial<Workspace>) =>
    getJSON<Workspace>('/api/workspaces', { method: 'POST', ...jsonBody(fields) }),
  updateWorkspace: (id: string, fields: Partial<Workspace>) =>
    getJSON<Workspace>(`/api/workspace/${encodeURIComponent(id)}`, { method: 'PUT', ...jsonBody(fields) }),
  deleteWorkspace: (id: string) =>
    getJSON<{ ok: boolean }>(`/api/workspace/${encodeURIComponent(id)}`, { method: 'DELETE' }),

  // upload / complete
  upload: (filename: string, contentBase64: string, opts?: { sessionId?: string | undefined; kbId?: string | undefined }) => {
    const body: { filename: string; content: string; session_id?: string; kb_id?: string } = {
      filename,
      content: contentBase64,
    };
    if (opts?.sessionId) body.session_id = opts.sessionId;
    if (opts?.kbId) body.kb_id = opts.kbId;
    return getJSON<import('./transport').Attachment>('/api/upload', { method: 'POST', ...jsonBody(body) });
  },
  complete: (query: string, opts?: { cwd?: string; workspaceId?: string }) => {
    const params = new URLSearchParams({ query });
    if (opts?.cwd) params.set('cwd', opts.cwd);
    if (opts?.workspaceId) params.set('workspace_id', opts.workspaceId);
    return getJSON<CompleteItem[]>(`/api/complete?${params}`);
  },

  // skills
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
};
