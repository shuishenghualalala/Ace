import type {
  AppConfig,
  Attachment,
  ExternalAgent,
  ExternalRuntime,
  ExternalTeam,
  ExternalTeamDraft,
  ExternalTeamDraftSlot,
  ExternalTeamRole,
  ExternalTeamSuggestion,
  FormationPlan,
  DebugEvent,
  RuntimeConcurrency,
  Scenario,
  CrewIntroLine,
  CrewLoadingStatus,
  Session,
  Skill,
  SkillStore,
  Task,
  TodoItem,
  WikiGraph,
  WikiKB,
  WikiPage,
  WikiRelationPage,
  WikiSourcePage,
  WikiVaultDocument,
  WikiSource,
  WikiSourceFiles,
  WikiSourceTitles,
  WikiUploadResult,
  Workspace,
} from "./types";

export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
    public url: string,
    public body?: { ok?: boolean; error?: string; error_code?: string; dependency?: string; install_command?: string; detail?: string },
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function getJSON<T>(url: string, opts?: RequestInit): Promise<T> {
  const res = await fetch(url, opts);
  if (!res.ok) {
    let body: ApiError["body"] | undefined;
    let detail = "";
    try {
      body = await res.json();
      detail = body?.error || body?.detail || JSON.stringify(body);
    } catch {
      try {
        detail = await res.text();
      } catch {
        detail = "";
      }
    }
    throw new ApiError(`${res.status} ${url}${detail ? `: ${detail}` : ""}`, res.status, url, body);
  }
  return res.json() as Promise<T>;
}

async function readNDJSON(
  response: Response,
  onValue: (value: unknown) => void,
): Promise<void> {
  if (!response.body) throw new Error("流式响应没有 body");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  const consumeLines = () => {
    const lines = buffer.split("\n");
    buffer = lines.pop() || "";
    lines.forEach((line) => {
      const text = line.trim();
      if (text) onValue(JSON.parse(text));
    });
  };

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      consumeLines();
    }
    buffer += decoder.decode();
    if (buffer.trim()) {
      const text = buffer.trim();
      buffer = "";
      onValue(JSON.parse(text));
    }
  } finally {
    reader.releaseLock();
  }
}

export type ExternalTeamDraftPhase = "initial" | "optimized" | "fallback";

export interface ExternalTeamDraftStreamMeta {
  llmElapsedMs?: number;
  cacheHit?: boolean;
}

interface ExternalTeamDraftStreamOptions {
  signal?: AbortSignal;
  onDraft?: (
    draft: ExternalTeamDraft,
    phase: ExternalTeamDraftPhase,
    meta: ExternalTeamDraftStreamMeta,
  ) => void;
  onDescriptionDelta?: (text: string) => void;
}

async function streamExternalTeamDraft(
  url: string,
  payload: { name?: string; description?: string; leader_agent_id?: string },
  options?: ExternalTeamDraftStreamOptions,
): Promise<ExternalTeamDraft> {
  const response = await fetch(url, {
    method: "POST",
    ...jsonBody(payload),
    signal: options?.signal,
  });
  if (!response.ok) throw new Error(`${response.status} ${url}`);
  let latest: ExternalTeamDraft | null = null;
  await readNDJSON(response, (value) => {
    const event = value as {
      type?: string;
      phase?: ExternalTeamDraftPhase;
      draft?: ExternalTeamDraft;
      text?: string;
      llm_elapsed_ms?: number;
      cache_hit?: boolean;
    };
    if (event.type === "description_delta" && typeof event.text === "string") {
      options?.onDescriptionDelta?.(event.text);
      return;
    }
    if (event.type !== "draft" || !event.draft || !event.phase) return;
    latest = event.draft;
    options?.onDraft?.(event.draft, event.phase, {
      llmElapsedMs: event.llm_elapsed_ms,
      cacheHit: event.cache_hit,
    });
  });
  if (!latest) throw new Error("团队草案流没有返回有效快照");
  return latest as ExternalTeamDraft;
}

export type ExternalTeamSuggestionPhase = "fast" | "ai_reviewing" | "final";

export interface ExternalTeamSuggestionStreamOptions {
  signal?: AbortSignal;
  onSuggestion?: (
    suggestion: ExternalTeamSuggestion,
    phase: "fast" | "final",
  ) => void;
  onStatus?: (phase: "ai_reviewing") => void;
}

async function streamExternalTeamSuggestion(
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
): Promise<ExternalTeamSuggestion> {
  const response = await fetch("/api/external-teams/suggest", {
    method: "POST",
    ...jsonBody({ ...payload, formation_mode: "auto" }),
    signal: options?.signal,
  });
  if (!response.ok) throw new Error(`${response.status} /api/external-teams/suggest`);
  let latest: ExternalTeamSuggestion | null = null;
  await readNDJSON(response, (value) => {
    const event = value as {
      type?: string;
      phase?: ExternalTeamSuggestionPhase;
      suggestion?: ExternalTeamSuggestion;
    };
    if (event.type === "status" && event.phase === "ai_reviewing") {
      options?.onStatus?.("ai_reviewing");
      return;
    }
    if (
      event.type === "suggestion"
      && (event.phase === "fast" || event.phase === "final")
      && event.suggestion
    ) {
      latest = event.suggestion;
      options?.onSuggestion?.(event.suggestion, event.phase);
    }
  });
  if (!latest) throw new Error("智能组队流没有返回有效方案");
  return latest as ExternalTeamSuggestion;
}

const jsonBody = (body: object): RequestInit => ({
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

const withKb = (url: string, kbId?: string) =>
  kbId ? `${url}${url.includes("?") ? "&" : "?"}kb_id=${encodeURIComponent(kbId)}` : url;

export const api = {
  config: () => getJSON<AppConfig>("/api/config"),
  switchModel: (modelId: string) =>
    getJSON<AppConfig>("/api/config/model", { method: "POST", ...jsonBody({ model_id: modelId }) }),
  // 会话
  sessions: () => getJSON<Session[]>("/api/sessions"),
  sessionsStatus: () => getJSON<Record<string, string>>("/api/sessions/status"),
  sessionStatus: (id: string) =>
    getJSON<{ session_id: string; live: string; queue_depth: number; last_status: string; last_error: string }>(
      `/api/session/${id}/status`,
    ),
  runtimeConcurrency: () => getJSON<RuntimeConcurrency>("/api/runtime/concurrency"),
  history: (id: string) =>
    getJSON<{ role: string; content: string; timestamp?: number; turn_started_at?: number; turn_duration?: number; thinking?: string; source_session_id?: string; agent_id?: string; agent_name?: string; agent_role?: string; agent_tone?: number; is_leader?: boolean; tool_calls?: {
      id: string;
      name: string;
      ui_label?: string;
      arguments: Record<string, unknown>;
      started_at?: number;
      duration?: number;
      result?: string;
      status?: "running" | "done" | "error";
    }[] }[]>(
      `/api/session/${id}`,
    ),
  sessionPlan: (id: string) =>
    getJSON<{
      session_id: string;
      active: boolean;
      awaiting_approval: boolean;
      phase?: string;
      status?: string;
      has_plan: boolean;
      plan: string;
      plan_file: string;
      options?: { label: string; description: string }[];
    }>(`/api/session/${id}/plan`),
  sessionTodos: (id: string) =>
    getJSON<{ todos: TodoItem[] }>(`/api/session/${id}/todos`),
  renameSession: (id: string, title: string) =>
    getJSON(`/api/session/${id}/title`, { method: "PUT", ...jsonBody({ title }) }),
  getSessionAgentConfig: (id: string) =>
    getJSON<Record<string, unknown>>(`/api/session/${id}/agent-config`),
  debugLog: (id: string, limit = 200) =>
    getJSON<{ enabled: boolean; events: DebugEvent[] }>(
      `/api/session/${id}/debug-log?limit=${limit}`,
    ),
  setSessionAgentConfig: (
    id: string,
    payload: { executor: "builtin" | "client" | "external" | "team"; [key: string]: unknown },
    options?: { workspace_id?: string; title?: string },
  ) =>
    getJSON<Record<string, unknown>>(`/api/session/${id}/agent-config`, {
      method: "PUT",
      ...jsonBody({ ...payload, ...(options || {}) }),
    }),
  deleteSession: (id: string) => getJSON(`/api/session/${id}`, { method: "DELETE" }),
  tasks: (id: string) => getJSON<Task[]>(`/api/tasks?session_id=${encodeURIComponent(id)}`),
  task: (id: string) => getJSON<Task>(`/api/tasks/${encodeURIComponent(id)}`),
  cancelTask: (id: string, reason = "用户取消") =>
    getJSON<Task>(`/api/tasks/${encodeURIComponent(id)}/cancel`, {
      method: "POST",
      ...jsonBody({ reason }),
    }),
  recoverTeamNode: (
    sessionId: string,
    nodeId: string,
    action: "reassign" | "retry" | "abandon",
    replacementAssignee = "",
  ) =>
    getJSON<{ ok: boolean; node?: Task; error?: string }>(
      `/api/session/${encodeURIComponent(sessionId)}/team/recover`,
      {
        method: "POST",
        ...jsonBody({
          node_id: nodeId,
          action,
          replacement_assignee: replacementAssignee,
        }),
      },
    ),
  waitTask: (id: string, timeout = 30) =>
    getJSON<Task>(`/api/tasks/${encodeURIComponent(id)}/wait`, {
      method: "POST",
      ...jsonBody({ timeout }),
    }),
  // Agent / 外部运行时
  scanRuntimes: () => getJSON<ExternalRuntime[]>("/api/runtimes/scan", { method: "POST" }),
  runtimes: () => getJSON<ExternalRuntime[]>("/api/runtimes"),
  registerRuntime: (runtime: Partial<ExternalRuntime>) =>
    getJSON<ExternalRuntime>("/api/runtimes/register", { method: "POST", ...jsonBody(runtime) }),
  externalAgents: () => getJSON<ExternalAgent[]>("/api/external-agents"),
  createExternalAgent: (agent: {
    name: string;
    runtime_id: string;
    model?: string;
    system_prompt?: string;
    instructions?: string;
    custom_args?: string[];
    custom_env?: Record<string, string>;
  }) => getJSON<ExternalAgent>("/api/external-agents", { method: "POST", ...jsonBody(agent) }),
  deleteExternalAgent: (id: string) =>
    getJSON<{ ok: boolean }>(`/api/external-agents/${encodeURIComponent(id)}`, { method: "DELETE" }),
  externalTeams: () => getJSON<ExternalTeam[]>("/api/external-teams"),
  externalTeamRoles: () => getJSON<ExternalTeamRole[]>("/api/external-teams/roles"),
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
  }) => getJSON<ExternalTeam>("/api/external-teams", { method: "POST", ...jsonBody(team) }),
  draftExternalTeamDescription: (
    payload: { name?: string },
    options?: ExternalTeamDraftStreamOptions,
  ) => streamExternalTeamDraft("/api/external-teams/draft/description", payload, options),
  draftExternalTeamFormation: (
    payload: { name?: string; description?: string; leader_agent_id?: string },
    options?: ExternalTeamDraftStreamOptions,
  ) => streamExternalTeamDraft("/api/external-teams/draft/formation", payload, options),
  suggestExternalTeam: (payload: {
    name?: string;
    description?: string;
    workflow?: string;
    leader_agent_id?: string;
    formation_mode: "fast" | "ai";
    slots?: ExternalTeamDraftSlot[];
    required_agent_ids?: string[];
    excluded_agent_ids?: string[];
    force_required_agent_ids?: string[];
    required_capabilities?: string[];
    custom_capabilities?: string[];
  }) =>
    getJSON<ExternalTeamSuggestion>("/api/external-teams/suggest", { method: "POST", ...jsonBody(payload) }),
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
  suggestExternalTeamRole: (payload: {
    name?: string;
    description?: string;
    workflow?: string;
    agent_id?: string;
    agent_name?: string;
    role_key: string;
    current_description?: string;
    is_leader?: boolean;
  }) =>
    getJSON<ExternalTeamRole & { role: string }>("/api/external-teams/roles/suggest", {
      method: "POST",
      ...jsonBody(payload),
    }),
  deleteExternalTeam: (id: string) =>
    getJSON<{ ok: boolean }>(`/api/external-teams/${encodeURIComponent(id)}`, { method: "DELETE" }),
  // 工作空间
  workspaces: () => getJSON<Workspace[]>("/api/workspaces"),
  createWorkspace: (w: Partial<Workspace>) =>
    getJSON<Workspace>("/api/workspaces", { method: "POST", ...jsonBody(w) }),
  updateWorkspace: (id: string, w: Partial<Workspace>) =>
    getJSON<Workspace>(`/api/workspace/${id}`, { method: "PUT", ...jsonBody(w) }),
  deleteWorkspace: (id: string) => getJSON(`/api/workspace/${id}`, { method: "DELETE" }),
  // 附件与上下文
  upload: (filename: string, contentBase64: string, opts?: { sessionId?: string; kbId?: string }) => {
    const body: { filename: string; content: string; session_id?: string; kb_id?: string } = {
      filename,
      content: contentBase64,
    };
    if (opts?.sessionId) body.session_id = opts.sessionId;
    if (opts?.kbId) body.kb_id = opts.kbId;
    return getJSON<Attachment>("/api/upload", { method: "POST", ...jsonBody(body) });
  },
  complete: (query: string) =>
    getJSON<{ text: string; display: string; meta: string; type: string }[]>(`/api/complete?query=${encodeURIComponent(query)}`),
  skills: () => getJSON<Skill[]>("/api/skills"),
  skillStore: () => getJSON<SkillStore>("/api/skills/store"),
  installSkill: (slug: string) =>
    getJSON<{ ok: boolean }>(`/api/skills/${encodeURIComponent(slug)}/install`, { method: "POST" }),
  uninstallSkill: (slug: string) =>
    getJSON<{ ok: boolean }>(`/api/skills/${encodeURIComponent(slug)}`, { method: "DELETE" }),
  scenarios: (count = 4) => getJSON<Scenario[]>(`/api/scenarios?count=${count}`),
  scenarioIntroLines: (count = 8) => getJSON<CrewIntroLine[]>(`/api/scenarios/intro-lines?count=${count}`),
  scenarioLoadingStatuses: (count = 8) => getJSON<CrewLoadingStatus[]>(`/api/scenarios/loading-status?count=${count}`),
  // Wiki
  wikiAgentSession: (kbId?: string, opts?: { forceNew?: boolean }) =>
    getJSON<{ ok: boolean; session_id: string; kb_id: string }>(
      `${withKb("/api/wiki/agent-session", kbId)}${opts?.forceNew ? `${kbId ? "&" : "?"}force_new=true` : ""}`,
      { method: "POST" },
    ),
  wikiAgentSessions: (kbId?: string) =>
    getJSON<{ ok: boolean; kb_id: string; sessions: Session[] }>(
      withKb("/api/wiki/agent-sessions", kbId),
    ),
  wikiKBs: () => getJSON<{ ok: boolean; kbs: WikiKB[] }>("/api/wiki/kbs"),
  wikiCreateKB: (payload: { kb_id: string; name?: string }) =>
    getJSON<{ ok: boolean; kb: WikiKB }>("/api/wiki/kbs", { method: "POST", ...jsonBody(payload) }),
  wikiDeleteKB: (kb_id: string) =>
    getJSON<{ ok: boolean; deleted_session_ids?: string[] }>(
      `/api/wiki/kbs/${encodeURIComponent(kb_id)}`,
      { method: "DELETE" },
    ),
  wikiVaultDocument: (name: "Home.md" | "index.md", kbId?: string) =>
    getJSON<{ ok: boolean; document: WikiVaultDocument }>(
      withKb(`/api/wiki/vault-documents/${encodeURIComponent(name)}`, kbId),
    ),
  wikiInit: (kbId?: string) =>
    getJSON<{ ok: boolean }>(withKb("/api/wiki/init", kbId), { method: "POST" }),
  wikiPages: (params?: { limit?: number; offset?: number; kb_id?: string; brief?: boolean }) =>
    getJSON<{ ok: boolean; pages: WikiPage[]; source_titles: WikiSourceTitles; source_files: WikiSourceFiles }>(
      `/api/wiki/pages?${new URLSearchParams({
        ...(params?.limit !== undefined ? { limit: String(params.limit) } : {}),
        ...(params?.offset !== undefined ? { offset: String(params.offset) } : {}),
        ...(params?.kb_id ? { kb_id: params.kb_id } : {}),
        brief: params?.brief === false ? "0" : "1",
      }).toString()}`,
    ),
  wikiPage: (id: string, kbId?: string) =>
    getJSON<{
      ok: boolean;
      page: WikiPage;
      source_titles: WikiSourceTitles;
      source_files: WikiSourceFiles;
      source_pages: WikiSourcePage[];
      relation_pages: WikiRelationPage[];
    }>(
      withKb(`/api/wiki/pages/${encodeURIComponent(id)}`, kbId),
    ),
  wikiDeletePage: (id: string, kbId?: string) =>
    getJSON<{ ok: boolean }>(withKb(`/api/wiki/pages/${encodeURIComponent(id)}`, kbId), { method: "DELETE" }),
  wikiDeletePages: (ids: string[], kbId?: string) =>
    getJSON<{ ok: boolean; deleted: string[]; failed: { id: string; error: string }[] }>(
      withKb("/api/wiki/pages", kbId),
      { method: "DELETE", ...jsonBody({ page_ids: ids }) },
    ),
  wikiUpload: (file: File, kbId?: string) => {
    const form = new FormData();
    form.append("file", file);
    return getJSON<WikiUploadResult>(withKb("/api/wiki/upload", kbId), {
      method: "POST",
      body: form,
    });
  },
  wikiSources: (params?: { status?: string; limit?: number; offset?: number; kb_id?: string }) =>
    getJSON<{ ok: boolean; sources: WikiSource[]; total: number; kb_id: string }>(
      `/api/wiki/sources?${new URLSearchParams({
        ...(params?.status ? { status: params.status } : {}),
        ...(params?.limit !== undefined ? { limit: String(params.limit) } : {}),
        ...(params?.offset !== undefined ? { offset: String(params.offset) } : {}),
        ...(params?.kb_id ? { kb_id: params.kb_id } : {}),
      }).toString()}`,
    ),
  wikiDeleteSource: (sourceId: string, kbId?: string) =>
    getJSON<{ ok: boolean; deleted_source_id: string; related_pages: { id: string; title: string }[] }>(
      withKb(`/api/wiki/sources/${encodeURIComponent(sourceId)}`, kbId),
      { method: "DELETE" },
    ),
  wikiQuery: (q: string, kbId?: string) =>
    getJSON<{ ok: boolean; text: string; pages: WikiPage[] }>(withKb(`/api/wiki/query?q=${encodeURIComponent(q)}`, kbId)),
  wikiSearch: (query: string, kbId?: string, topK = 5) =>
    getJSON<{ ok: boolean; pages: WikiPage[]; source_titles: WikiSourceTitles; source_files: WikiSourceFiles }>(
      withKb(`/api/wiki/search?q=${encodeURIComponent(query)}&top_k=${topK}`, kbId),
    ),
  wikiGraph: (kbId?: string) =>
    getJSON<{ ok: boolean; graph: WikiGraph }>(withKb("/api/wiki/graph", kbId)),
  wikiCompile: (kbId?: string) =>
    getJSON<{ ok: boolean; ingested: string[]; errors: string[] }>(withKb("/api/wiki/compile", kbId), { method: "POST" }),
  wikiLint: (kbId?: string) =>
    getJSON<{ ok: boolean; issues: Record<string, any>[] }>(withKb("/api/wiki/lint", kbId), { method: "POST" }),
  wikiIngest: (source_id: string, kbId?: string, session_id?: string) =>
    getJSON<{ ok: boolean; source_id: string; pages: WikiPage[]; issues: string[] }>(withKb("/api/wiki/ingest", kbId), {
      method: "POST",
      ...jsonBody({ source_id, session_id }),
    }),
  wikiCancelIngest: (source_id: string, kbId?: string) =>
    getJSON<{ ok: boolean; cancelled?: boolean }>(withKb("/api/wiki/ingest/cancel", kbId), {
      method: "POST",
      ...jsonBody({ source_id }),
    }),
  wikiSourceFileUrl: (sourceId: string, kbId?: string) =>
    withKb(`/api/wiki/sources/${encodeURIComponent(sourceId)}/file`, kbId),
};
